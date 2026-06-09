"""Coordinator overlay on top of attempt caps (ADR-0084 §12 / Task 2B).

The reactive attempt caps in :mod:`triggers` (``_is_capped``) remain the
hard backstop: they bound runaway dispatch when no oversight is present.
ADR-0084 introduces a coordinator session that watches plan-scoped events
and may set ``task_board.status = "blocked_operator"`` on a task that has
escalated to the operator instance for strategic decision. While a task
sits in that state, dispatch should stop — the operator is mid-thought,
and burning further attempts is pure waste even if we're still under
the cap.

This module exposes one helper, :func:`coordinator_overlay_decision`,
that the two ``_is_capped`` callsites consult BEFORE the existing cap
body. The decision is one of:

* :attr:`CapOverlayDecision.BLOCK_BY_COORDINATOR` — the plan has at
  least one ``blocked_operator`` row AND the coordinator is alive (the
  most recent ``task_board.updated_at`` for this plan is within the
  liveness threshold). Caller skips dispatch entirely; no cap check.
* :attr:`CapOverlayDecision.COORDINATOR_ABSENT` — either no
  ``task_board`` rows for the plan (never reconciled) or the most
  recent ``updated_at`` is older than the liveness threshold (a
  previously-live coordinator has gone quiet). Caller falls through
  to the cap check; caps fire normally when the coordinator can't
  exercise judgment.
* :attr:`CapOverlayDecision.FALL_THROUGH` — coordinator is alive and no
  ``blocked_operator`` rows on the plan. Caller proceeds to the cap
  check; caps stay armed as the backstop.

The overlay is purely additive: caps remain hard-stop. The overlay is
an earlier-stop mechanism when the coordinator is actively gating, and
a faster-fail mechanism when the coordinator is missing.

Caching
-------
Each decision is cached per ``plan_id`` for :data:`OVERLAY_CACHE_TTL_S`
seconds (default 30s). The two ``_is_capped`` callsites run on the
dispatch hot path — for a busy plan, a per-dispatch DB query would add
N round-trips per fanout. The cache absorbs that without hiding state
changes for more than the TTL.

Invalidation
~~~~~~~~~~~~
:func:`invalidate_overlay_cache` is called from ``PATCH /api/v1/task_board``
after a successful commit so a coordinator's ``blocked_operator`` write
is visible to the next cap check immediately. Without that, a freshly-
escalated task could still dispatch for up to ``OVERLAY_CACHE_TTL_S``
because the helper would serve a stale FALL_THROUGH from cache.

Test fixtures should call :func:`_clear_overlay_cache` between test
runs; the module-level dict is shared process state and would
otherwise leak across tests.

Logging discipline
------------------
* The "no rows yet" path logs at DEBUG. Early-plan state before the
  coordinator's first reconciliation is normal; an INFO/WARNING line
  per dispatch would drown out the startup window.
* The "rows existed but updated_at is stale" path logs at WARNING.
  A previously-live coordinator going quiet is exactly the signal ops
  wants to grep for.
"""

from __future__ import annotations

import enum
import logging
import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models import Task
from treadmill_api.models.task_board import TaskBoard

logger = logging.getLogger("treadmill.coordination.overlay")


# ── Tunables ──────────────────────────────────────────────────────────────────
# Threshold past which a plan's task_board is considered stale and the
# coordinator considered absent. 15 minutes matches the impl plan §3A risk
# note: long enough that a coordinator briefly busy with a long architect
# call doesn't accidentally retire its oversight, short enough that a
# truly-dead coordinator doesn't strand the plan for hours under hard cap.
COORDINATOR_LIVENESS_THRESHOLD_S = 900

# Per-plan cache TTL. Short enough that a real status change is visible
# within one half-minute even if the PATCH→invalidate path is bypassed,
# long enough to absorb the dispatch hot path of a busy plan without
# hitting the DB on every cap check.
OVERLAY_CACHE_TTL_S = 30


class CapOverlayDecision(enum.Enum):
    """The three states the cap overlay can be in for one plan."""

    BLOCK_BY_COORDINATOR = "block_by_coordinator"
    """At least one task in this plan is ``blocked_operator`` AND the
    coordinator is alive. Caller MUST skip dispatch; do not consult the
    attempt cap. The coordinator has already escalated."""

    COORDINATOR_ABSENT = "coordinator_absent"
    """No task_board rows for the plan OR the most recent ``updated_at``
    is older than :data:`COORDINATOR_LIVENESS_THRESHOLD_S`. Caller falls
    through to the existing cap check; caps fire normally."""

    FALL_THROUGH = "fall_through"
    """Coordinator alive, no ``blocked_operator`` rows. Caller proceeds
    to the existing cap check; caps remain armed as backstop."""


# ── Cache ─────────────────────────────────────────────────────────────────────
# Value = (decision, expires_at_monotonic). monotonic() is the right clock for
# expiry — wall-clock skew under NTP correction would otherwise cause a "wrong
# direction" jump that could either over- or under-expire entries.
_overlay_cache: dict[uuid.UUID, tuple[CapOverlayDecision, float]] = {}


def invalidate_overlay_cache(plan_id: uuid.UUID) -> None:
    """Drop the cached decision for ``plan_id`` so the next caller hits
    the DB. Idempotent. Called from ``PATCH /api/v1/task_board`` so a
    coordinator's status change is visible to the dispatch path
    immediately."""
    _overlay_cache.pop(plan_id, None)


def _clear_overlay_cache() -> None:
    """Test hook: wipe the entire cache between test functions so module-
    level state doesn't leak across cases. Not for production use."""
    _overlay_cache.clear()


async def coordinator_overlay_decision(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> CapOverlayDecision:
    """Decide whether the coordinator overlay should block dispatch,
    fall through to caps, or short-circuit to caps because the
    coordinator is absent.

    Resolves the task's ``plan_id`` from the ``tasks`` row, then queries
    ``task_board`` for the plan in a single round-trip that returns
    both the maximum ``updated_at`` and a boolean ``"any row has
    status = 'blocked_operator'"``. The decision is cached per plan for
    :data:`OVERLAY_CACHE_TTL_S` seconds.

    Args:
        session: live :class:`AsyncSession`.
        task_id: the task whose dispatch is being considered.

    Returns:
        One of the three :class:`CapOverlayDecision` values. Callers
        gate on ``BLOCK_BY_COORDINATOR`` (skip dispatch) and proceed to
        the existing cap logic on either of the other two.
    """
    task = await session.get(Task, task_id)
    if task is None:
        # Defensive: the cap callsites always have a real task in hand,
        # but a missing row means we can't reason about the plan — let
        # the cap path handle it.
        logger.debug(
            "overlay: task %s not found; falling through to cap check",
            task_id,
        )
        return CapOverlayDecision.FALL_THROUGH

    plan_id = task.plan_id
    cached = _overlay_cache.get(plan_id)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]

    # One round-trip: MAX(updated_at) and bool_or(status='blocked_operator')
    # for the plan. Both come back as scalars; NULLs propagate when there
    # are zero rows.
    result = await session.execute(
        select(
            func.max(TaskBoard.updated_at),
            func.bool_or(TaskBoard.status == "blocked_operator"),
        ).where(TaskBoard.plan_id == plan_id)
    )
    max_updated_at, any_blocked = result.one()

    decision = _classify(
        plan_id=plan_id,
        max_updated_at=max_updated_at,
        any_blocked=bool(any_blocked) if any_blocked is not None else False,
    )
    _overlay_cache[plan_id] = (decision, now + OVERLAY_CACHE_TTL_S)
    return decision


def _classify(
    *,
    plan_id: uuid.UUID,
    max_updated_at,
    any_blocked: bool,
) -> CapOverlayDecision:
    """Pure mapping from the SQL result to a decision + the log lines
    that go with each branch. Separated out so the cache wrapper above
    stays focused on caching and the test surface for log-level
    discipline can be exercised without a DB."""
    if max_updated_at is None:
        # No task_board rows at all for this plan. Normal early-plan
        # state — coordinator hasn't reconciled yet. DEBUG, not WARNING.
        logger.debug(
            "overlay: plan %s has no task_board rows; coordinator absent "
            "(pre-reconciliation)",
            plan_id,
        )
        return CapOverlayDecision.COORDINATOR_ABSENT

    age_s = _age_seconds(max_updated_at)
    if age_s > COORDINATOR_LIVENESS_THRESHOLD_S:
        # Previously-live coordinator gone quiet. THIS is the line ops
        # wants to grep.
        logger.warning(
            "overlay: plan %s task_board stale by %.0fs (threshold %ds); "
            "coordinator absent — caps will fire normally",
            plan_id,
            age_s,
            COORDINATOR_LIVENESS_THRESHOLD_S,
        )
        return CapOverlayDecision.COORDINATOR_ABSENT

    if any_blocked:
        logger.info(
            "overlay: plan %s has a blocked_operator task; cap check skipped — "
            "coordinator is gating dispatch",
            plan_id,
        )
        return CapOverlayDecision.BLOCK_BY_COORDINATOR

    return CapOverlayDecision.FALL_THROUGH


def _age_seconds(ts) -> float:
    """Compute the age of a TIMESTAMP-with-timezone value in seconds
    against wall-clock now. Wall-clock is the right clock here because
    ``updated_at`` is itself wall-clock (``server_default=now()``) and
    we want operator-perceived staleness — a system that hasn't NTP'd
    in days SHOULD show a stale coordinator."""
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds()
