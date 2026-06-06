"""Fleet-wedge sweep — detect workers=0 while a family's queue is depended-on.

ADR-0075 §3 v1: only the zero-workers sub-signal at this iteration.
zero-consume-rate and spawn-failure-rate sub-signals are follow-ups.

The autoscaler writes per-family heartbeats to ``system_status`` (per the
ADR-0075 system-status task). This sweep reads those rows and emits a
``system.fleet_wedged`` event when a family has zero workers AND its
``last_spawn_at`` was recent enough to indicate the family is meant to
be active (i.e. we want to alert on "should have workers but doesn't",
not "deliberately scaled to zero and never used since").

The detector is deterministic and runs on the existing scheduled-tick
machinery — no role-step, no Claude call. Sibling shape to
``step_starvation_sweep``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("treadmill.coordination.fleet_wedge_sweep")


FLEET_WEDGE_SWEEP_WORKFLOW_ID = "wf-fleet-wedge-sweep"
"""Schedule's ``workflow_id`` value. ``handle_scheduled_tick`` intercepts
this slug and runs the deterministic detector instead of looking up a
``WorkflowVersion`` (there is none — this bot is a query, not a role)."""


FLEET_WEDGE_ZERO_WORKERS_THRESHOLD = timedelta(minutes=3)
"""How long a family can sit at worker_count == 0 with a recent spawn
attempt before the sweep emits ``system.fleet_wedged``. Tuned short
enough that the 2026-06-05 autoscaler-wedge incident (zero workers for
1h26m before operator noticed) would have alerted within minutes, long
enough that a clean ``treadmill-local down`` doesn't trip the detector
on every shutdown."""


FLEET_WEDGE_ACTIVITY_WINDOW = timedelta(hours=1)
"""Only families whose ``last_spawn_at`` is within this window are
considered candidates for the zero-workers signal. Stale families
(autoscaler scaled to zero hours ago with no fresh spawn intent) are
excluded so we don't alert on a deliberately-quiescent family. This is
the operationally-honest version of the ``autoscaler.scaled_to_zero``
shutdown hint the ADR proposes — it requires no extra state, just
relies on the autoscaler's own spawn cadence."""


FLEET_WEDGE_SIGNAL = "wf-fleet-wedge-sweep-zero-workers"
"""Dedup key. A second sweep tick on the same wedge episode reads the
existing event row and no-ops."""


_FLEET_WEDGE_ZERO_WORKERS_SQL = text("""
    SELECT
        family,
        worker_count,
        last_spawn_at,
        last_spawn_error,
        consecutive_spawn_failures,
        updated_at
    FROM system_status
    WHERE worker_count = 0
      AND last_spawn_at IS NOT NULL
      AND last_spawn_at > :activity_cutoff
      AND updated_at < :wedge_cutoff
""")
"""Find families with worker_count=0 AND a recent spawn intent (within
``FLEET_WEDGE_ACTIVITY_WINDOW``) AND a stale-enough heartbeat that the
zero state has held for at least ``FLEET_WEDGE_ZERO_WORKERS_THRESHOLD``.

The ``updated_at < :wedge_cutoff`` clause is the load-bearing one: it
means "the autoscaler last reported worker_count=0 more than
:wedge_cutoff ago, and the count has been zero since (heartbeat doesn't
change when state doesn't change)." Equivalently: "we've been at zero
workers for at least the threshold."
"""


async def run_fleet_wedge_sweep(
    session: AsyncSession,
    dispatcher: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Detect families wedged at workers=0 and emit ``system.fleet_wedged``.

    Returns the count of events emitted on this tick. ``now`` is
    overridable for tests; production callers pass ``None`` and the
    sweep clocks itself.

    The sweep is a pure read followed by a series of single-event
    INSERTs via the dispatcher's ``persist_and_publish`` helper. No new
    ``WorkflowRun`` is materialized — this bot is wired straight onto
    the scheduled tick.

    Dedup is per-family-per-episode: a wedge that's still open (no
    later resolution event) doesn't re-fire. Once the autoscaler
    reports a non-zero worker_count, the wedge is "resolved" by virtue
    of falling out of the SQL result set, and a future zero-state would
    fire fresh.
    """
    if dispatcher is None:
        return 0

    moment = now if now is not None else datetime.now(timezone.utc)
    wedge_cutoff = moment - FLEET_WEDGE_ZERO_WORKERS_THRESHOLD
    activity_cutoff = moment - FLEET_WEDGE_ACTIVITY_WINDOW

    result = await session.execute(
        _FLEET_WEDGE_ZERO_WORKERS_SQL,
        {"wedge_cutoff": wedge_cutoff, "activity_cutoff": activity_cutoff},
    )
    rows = list(result)

    if not rows:
        logger.debug(
            "fleet-wedge sweep: no wedged families at %s",
            moment.isoformat(),
        )
        return 0

    emitted = 0
    for row in rows:
        try:
            # Dedup: skip if a system.fleet_wedged event for this family
            # already exists with no later "resolved" signal. The minimal
            # dedup for v1 is "any system.fleet_wedged event for this
            # family in the last FLEET_WEDGE_ACTIVITY_WINDOW" — a more
            # nuanced open/close lifecycle is a follow-up. The narrow
            # window mirrors the candidate-family activity filter so
            # we don't reincarnate ancient wedges.
            existing = await session.execute(text("""
                SELECT 1 FROM events
                WHERE entity_type = 'system'
                  AND action = 'fleet_wedged'
                  AND payload->>'family' = :family
                  AND payload->>'sub_signal' = :sub_signal
                  AND created_at > :activity_cutoff
                LIMIT 1
            """), {
                "family": row.family,
                "sub_signal": "zero-workers",
                "activity_cutoff": activity_cutoff,
            })
            if existing.first() is not None:
                continue

            seconds_wedged = (moment - row.updated_at).total_seconds()
            payload = {
                "family": row.family,
                "sub_signal": "zero-workers",
                "worker_count": row.worker_count,
                "last_spawn_at": (
                    row.last_spawn_at.isoformat()
                    if row.last_spawn_at else None
                ),
                "last_spawn_error": row.last_spawn_error,
                "consecutive_spawn_failures": row.consecutive_spawn_failures,
                "seconds_wedged": int(seconds_wedged),
                "signal": FLEET_WEDGE_SIGNAL,
                "remediation_path": "docs/playbooks/image-build-stuck.md",
            }
            await dispatcher.persist_and_publish(
                session,
                entity_type="system",
                action="fleet_wedged",
                payload=payload,
            )
            emitted += 1
            logger.warning(
                "fleet-wedge: emitted system.fleet_wedged for family=%s "
                "(workers=0 for %.0fs; last_spawn_error=%r)",
                row.family, seconds_wedged, row.last_spawn_error,
            )
        except Exception:
            logger.exception(
                "fleet-wedge sweep: emission failed for family %s; continuing",
                row.family,
            )

    logger.info(
        "fleet-wedge sweep: emitted %d/%d fleet_wedged event(s)",
        emitted, len(rows),
    )
    return emitted
