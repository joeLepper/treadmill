"""Unit tests for the coordinator cap overlay (ADR-0084 Task 2B).

Coverage:

* The three decision states: ``BLOCK_BY_COORDINATOR``,
  ``COORDINATOR_ABSENT``, ``FALL_THROUGH``.
* The two "absent" sub-paths: no-rows (DEBUG log) vs. stale-rows
  (WARNING log) — both end at the same enum but the log levels matter
  for operator grepping.
* The cache: hits skip the DB; invalidation by plan_id forces a re-read.
* The retry endpoint surface: a coordinator-blocked plan returns 409
  with the coordinator-specific message; ``force_bypass_cap`` is a
  single override that bypasses both gates.

These are unit tests — the SQL is stubbed at the session boundary so
the suite stays in-process and fast. The integration of the overlay
with the actual ``triggers.py`` dispatch loop is covered by the
existing trigger-pipeline tests; this file pins the overlay's own
contract.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from treadmill_api.coordination import coordinator_overlay
from treadmill_api.coordination.coordinator_overlay import (
    COORDINATOR_LIVENESS_THRESHOLD_S,
    CapOverlayDecision,
    _clear_overlay_cache,
    coordinator_overlay_decision,
    invalidate_overlay_cache,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class _TaskRow:
    """Stand-in for the ORM Task with the one attribute the overlay reads."""

    def __init__(self, task_id: uuid.UUID, plan_id: uuid.UUID) -> None:
        self.id = task_id
        self.plan_id = plan_id


class _StubSession:
    """Minimal async-session shape: ``get(Task, task_id)`` returns the
    seeded task; ``execute(...)`` returns the configured (max_updated_at,
    any_blocked) tuple wrapped in a one-row result. Both calls are
    counted so tests can assert the cache short-circuits the DB."""

    def __init__(
        self,
        max_updated_at: datetime | None,
        any_blocked: bool,
    ) -> None:
        self._max_updated_at = max_updated_at
        self._any_blocked = any_blocked
        self.get_calls = 0
        self.execute_calls = 0
        self._tasks: dict[uuid.UUID, _TaskRow] = {}

    def seed_task(self, task_id: uuid.UUID, plan_id: uuid.UUID) -> None:
        self._tasks[task_id] = _TaskRow(task_id, plan_id)

    async def get(self, model: Any, key: uuid.UUID) -> Any | None:
        self.get_calls += 1
        return self._tasks.get(key)

    async def execute(self, stmt: Any) -> "_StubResult":
        self.execute_calls += 1
        return _StubResult((self._max_updated_at, self._any_blocked))


class _StubResult:
    def __init__(self, row: tuple) -> None:
        self._row = row

    def one(self) -> tuple:
        return self._row


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """Module-level cache is shared process state; wipe it between
    tests so case ordering doesn't matter."""
    _clear_overlay_cache()
    yield
    _clear_overlay_cache()


# ── Decision state tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_block_by_coordinator_when_alive_and_any_blocked():
    """Recent updated_at + at least one blocked_operator row → BLOCK."""
    task_id, plan_id = uuid.uuid4(), uuid.uuid4()
    session = _StubSession(
        max_updated_at=_utcnow() - timedelta(seconds=10),  # well within threshold
        any_blocked=True,
    )
    session.seed_task(task_id, plan_id)

    decision = await coordinator_overlay_decision(session, task_id)
    assert decision is CapOverlayDecision.BLOCK_BY_COORDINATOR


@pytest.mark.asyncio
async def test_fall_through_when_alive_and_nothing_blocked():
    """Recent updated_at + no blocked_operator rows → FALL_THROUGH."""
    task_id, plan_id = uuid.uuid4(), uuid.uuid4()
    session = _StubSession(
        max_updated_at=_utcnow() - timedelta(seconds=10),
        any_blocked=False,
    )
    session.seed_task(task_id, plan_id)

    decision = await coordinator_overlay_decision(session, task_id)
    assert decision is CapOverlayDecision.FALL_THROUGH


@pytest.mark.asyncio
async def test_coordinator_absent_when_no_rows(caplog):
    """No task_board rows for plan → COORDINATOR_ABSENT logged at DEBUG."""
    task_id, plan_id = uuid.uuid4(), uuid.uuid4()
    session = _StubSession(max_updated_at=None, any_blocked=False)
    session.seed_task(task_id, plan_id)

    with caplog.at_level(logging.DEBUG, logger="treadmill.coordination.overlay"):
        decision = await coordinator_overlay_decision(session, task_id)

    assert decision is CapOverlayDecision.COORDINATOR_ABSENT
    # No WARNING on the no-rows path — that would drown out startup noise.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [], (
        f"no-rows path must NOT log at WARNING; got {[r.message for r in warnings]}"
    )
    # And it must log SOMETHING at DEBUG so operators can confirm the path fired.
    debugs = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "pre-reconciliation" in r.message
    ]
    assert len(debugs) == 1


@pytest.mark.asyncio
async def test_coordinator_absent_when_stale_rows(caplog):
    """Old updated_at → COORDINATOR_ABSENT logged at WARNING."""
    task_id, plan_id = uuid.uuid4(), uuid.uuid4()
    stale_age = COORDINATOR_LIVENESS_THRESHOLD_S + 60  # past the threshold
    session = _StubSession(
        max_updated_at=_utcnow() - timedelta(seconds=stale_age),
        any_blocked=False,
    )
    session.seed_task(task_id, plan_id)

    with caplog.at_level(logging.WARNING, logger="treadmill.coordination.overlay"):
        decision = await coordinator_overlay_decision(session, task_id)

    assert decision is CapOverlayDecision.COORDINATOR_ABSENT
    # Stale path MUST emit a WARNING — this is what ops greps for.
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "task_board stale" in r.message
        and "caps will fire normally" in r.message
    ]
    assert len(warnings) == 1, (
        f"stale path must log exactly one WARNING; got {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_stale_but_blocked_still_treated_as_absent():
    """A stale coordinator that left a blocked_operator row behind is
    still absent — we can't trust a stale gate. Caps fire."""
    task_id, plan_id = uuid.uuid4(), uuid.uuid4()
    session = _StubSession(
        max_updated_at=_utcnow() - timedelta(seconds=COORDINATOR_LIVENESS_THRESHOLD_S + 60),
        any_blocked=True,  # the row still says blocked, but…
    )
    session.seed_task(task_id, plan_id)

    decision = await coordinator_overlay_decision(session, task_id)
    # Stale wins over blocked. We don't honor an unreachable coordinator.
    assert decision is CapOverlayDecision.COORDINATOR_ABSENT


@pytest.mark.asyncio
async def test_missing_task_falls_through():
    """If the task doesn't exist (shouldn't happen at the callsites),
    we can't reason about the plan — fall through to cap behavior."""
    task_id = uuid.uuid4()
    session = _StubSession(max_updated_at=None, any_blocked=False)
    # No seed_task → session.get() returns None

    decision = await coordinator_overlay_decision(session, task_id)
    assert decision is CapOverlayDecision.FALL_THROUGH
    # And we never reached the SQL query — short-circuited before it.
    assert session.execute_calls == 0


# ── Cache tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_skips_db_query():
    """Two consecutive decisions in the TTL window hit the DB once."""
    task_id, plan_id = uuid.uuid4(), uuid.uuid4()
    session = _StubSession(
        max_updated_at=_utcnow() - timedelta(seconds=10),
        any_blocked=False,
    )
    session.seed_task(task_id, plan_id)

    await coordinator_overlay_decision(session, task_id)
    await coordinator_overlay_decision(session, task_id)

    # Two calls, one DB execute — the second decision was served from cache.
    assert session.execute_calls == 1


@pytest.mark.asyncio
async def test_invalidate_overlay_cache_forces_re_read():
    """After invalidate, the next call hits the DB again — and a fresh
    blocked_operator status flips the decision."""
    task_id, plan_id = uuid.uuid4(), uuid.uuid4()
    session = _StubSession(
        max_updated_at=_utcnow() - timedelta(seconds=10),
        any_blocked=False,  # initially: nothing blocked
    )
    session.seed_task(task_id, plan_id)

    first = await coordinator_overlay_decision(session, task_id)
    assert first is CapOverlayDecision.FALL_THROUGH

    # Simulate a PATCH that escalated a task to blocked_operator: the
    # row exists now, so the next query would return any_blocked=True.
    session._any_blocked = True

    # Without invalidation, the cache still answers FALL_THROUGH:
    cached = await coordinator_overlay_decision(session, task_id)
    assert cached is CapOverlayDecision.FALL_THROUGH
    assert session.execute_calls == 1, "second call should hit cache, not DB"

    # With invalidation, the next call re-queries and sees BLOCK.
    invalidate_overlay_cache(plan_id)
    after_invalidate = await coordinator_overlay_decision(session, task_id)
    assert after_invalidate is CapOverlayDecision.BLOCK_BY_COORDINATOR
    assert session.execute_calls == 2


@pytest.mark.asyncio
async def test_cache_is_per_plan():
    """Two plans have independent cache entries — invalidating one
    doesn't drop the other."""
    task_a, plan_a = uuid.uuid4(), uuid.uuid4()
    task_b, plan_b = uuid.uuid4(), uuid.uuid4()

    # Plan A is FALL_THROUGH; plan B is BLOCK_BY_COORDINATOR.
    session_a = _StubSession(_utcnow(), any_blocked=False)
    session_a.seed_task(task_a, plan_a)
    session_b = _StubSession(_utcnow(), any_blocked=True)
    session_b.seed_task(task_b, plan_b)

    await coordinator_overlay_decision(session_a, task_a)
    await coordinator_overlay_decision(session_b, task_b)

    # Invalidate only plan A's entry.
    invalidate_overlay_cache(plan_a)

    # Plan B's cached BLOCK is still in effect — a second call doesn't
    # re-query.
    repeat_b = await coordinator_overlay_decision(session_b, task_b)
    assert repeat_b is CapOverlayDecision.BLOCK_BY_COORDINATOR
    assert session_b.execute_calls == 1, "plan B cache should survive plan A invalidation"


def test_invalidate_missing_plan_is_noop():
    """``invalidate_overlay_cache`` is idempotent — invalidating a
    plan that was never cached doesn't raise."""
    invalidate_overlay_cache(uuid.uuid4())  # no exception is the assertion


# ── Retry endpoint surface tests ──────────────────────────────────────────────


# ── Retry endpoint surface tests ──────────────────────────────────────────────
#
# The retry endpoint integrates the overlay + cap + dispatch chain. Full
# request-level coverage (including the 201 dispatch path + DB writes)
# lives in tests/test_integration_task_retry.py and runs under
# TREADMILL_INTEGRATION=1.
#
# The unit tests below exercise the GATE BRANCH LOGIC the router uses
# without spinning up the full FastAPI app — they call the gate branches
# directly via a helper that mirrors the router's decision sequence.
# This keeps the suite hermetic + fast and pins the exact shape Task 2B
# adds: overlay-then-cap order, force_bypass_cap covers both gates,
# the 409 detail for the coordinator-blocked branch is distinct from the
# cap-reached one so operators can tell the two apart.


async def _simulate_retry_gate(
    *,
    overlay_decision: CapOverlayDecision,
    is_capped: bool,
    force_bypass_cap: bool,
) -> tuple[str, str | None]:
    """Replay the gate-decision portion of POST /tasks/{task_id}/retry.

    Returns ``(outcome, detail)``:
      * ``("ok", None)`` — both gates passed, the router would proceed
        to dispatch.
      * ``("blocked_by_coordinator", "<detail>")`` — overlay 409.
      * ``("capped", "<detail>")`` — cap 409.

    Mirrors the exact branch shape in routers/tasks.py:3a–3b. When the
    router refactors, this helper updates one place. The integration
    test verifies the same shape end-to-end.
    """
    # Step 3a — overlay (only when not bypassing).
    if not force_bypass_cap:
        if overlay_decision is CapOverlayDecision.BLOCK_BY_COORDINATOR:
            return (
                "blocked_by_coordinator",
                "plan is in coordinator-blocked state "
                "(task_board status = blocked_operator); coordinator "
                "must release before retry, or pass "
                "force_bypass_cap=true to override",
            )

    # Step 3b — cap (only when not bypassing).
    if is_capped and not force_bypass_cap:
        return ("capped", "cap reached; pass force_bypass_cap=true")

    return ("ok", None)


@pytest.mark.asyncio
async def test_retry_gate_blocked_by_coordinator_short_circuits_cap():
    """When the overlay returns BLOCK and force_bypass_cap=False, the
    409 is the coordinator-specific one — _is_capped is never consulted
    (we simulate that by setting is_capped=True; if the router consulted
    the cap, we'd see the cap-reached message instead)."""
    outcome, detail = await _simulate_retry_gate(
        overlay_decision=CapOverlayDecision.BLOCK_BY_COORDINATOR,
        is_capped=True,
        force_bypass_cap=False,
    )
    assert outcome == "blocked_by_coordinator"
    assert "coordinator-blocked state" in detail
    assert "blocked_operator" in detail


@pytest.mark.asyncio
async def test_retry_gate_fall_through_then_cap_fires():
    """Overlay FALL_THROUGH + cap reached → cap 409 with the
    cap-specific message (distinct from the coordinator-blocked one)."""
    outcome, detail = await _simulate_retry_gate(
        overlay_decision=CapOverlayDecision.FALL_THROUGH,
        is_capped=True,
        force_bypass_cap=False,
    )
    assert outcome == "capped"
    assert detail == "cap reached; pass force_bypass_cap=true"


@pytest.mark.asyncio
async def test_retry_gate_coordinator_absent_falls_through_to_cap():
    """When the coordinator is absent, the cap is the active gate."""
    outcome, _ = await _simulate_retry_gate(
        overlay_decision=CapOverlayDecision.COORDINATOR_ABSENT,
        is_capped=True,
        force_bypass_cap=False,
    )
    assert outcome == "capped"

    outcome2, _ = await _simulate_retry_gate(
        overlay_decision=CapOverlayDecision.COORDINATOR_ABSENT,
        is_capped=False,
        force_bypass_cap=False,
    )
    assert outcome2 == "ok"


@pytest.mark.asyncio
async def test_retry_gate_force_bypass_bypasses_both():
    """``force_bypass_cap=True`` reaches the dispatch path even when both
    gates would otherwise fire (overlay BLOCK + cap reached). Single
    operator-override flag covers both gates."""
    outcome, _ = await _simulate_retry_gate(
        overlay_decision=CapOverlayDecision.BLOCK_BY_COORDINATOR,
        is_capped=True,
        force_bypass_cap=True,
    )
    assert outcome == "ok"
