"""Step-starvation sweep — detect step.ready with no step.started.

When a step transitions to ``ready`` (queued for worker dispatch) but never
reaches ``started`` (worker picked it up), the task stalls silently. A worker
may have crashed, the queue may be stuck, or the SQS message may be lost. Per
ADR-0075, the sweep detects steps older than the threshold with no later
``step.started`` and escalates them to operator.

The ``*/1`` ``wf-step-starvation-sweep`` schedule fires this deterministic
detector on every minute. The signal — ready event older than
``STEP_STARVATION_THRESHOLD`` with no downstream ``started`` — indicates a
stalled dispatch queue that the operator must resolve.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("treadmill.coordination.step_starvation_sweep")


STEP_STARVATION_SWEEP_WORKFLOW_ID = "wf-step-starvation-sweep"
"""Schedule's ``workflow_id`` value. ``handle_scheduled_tick`` intercepts
this slug and runs the deterministic detector instead of looking up a
``WorkflowVersion`` (there is none — this bot is a query, not a role)."""


STEP_STARVATION_THRESHOLD = timedelta(minutes=5)
"""How long a step can sit in ``ready`` state before the sweep escalates it.
Tuned short enough that operator intervention is prompt, long enough that
legitimate queue latency (e.g. worker startup, queue drainage) doesn't
trip the detector."""


STEP_STARVATION_SIGNAL = "wf-step-starvation-sweep-stalled"
"""The ``last_verdict`` stamped on the escalation event. The generic
emitter's ``(task_id, signal)`` dedup uses this as the key — a second
sweep tick on the same stall reads the existing row and no-ops."""


_STARVATION_STEPS_SQL = text("""
    WITH step_events AS (
        SELECT
            task_id,
            (payload->>'step_index')::int AS step_index,
            action AS step_action,
            payload->>'step_name' AS step_name,
            payload->>'role_id' AS role_id,
            created_at AS event_at
        FROM events
        WHERE entity_type = 'step'
    ),
    latest_step_event AS (
        SELECT DISTINCT ON (task_id, step_index)
            task_id,
            step_index,
            step_action,
            step_name,
            role_id,
            event_at
        FROM step_events
        ORDER BY task_id, step_index, event_at DESC
    )
    SELECT t.id AS task_id,
           t.repo AS repo,
           lse.step_name,
           lse.role_id,
           lse.event_at AS ready_at
    FROM tasks t
    JOIN latest_step_event lse ON lse.task_id = t.id
    WHERE lse.step_action = 'ready'
      AND lse.event_at < :cutoff
      AND NOT EXISTS (
          SELECT 1 FROM events ev
          WHERE ev.task_id = t.id
            AND (
                (ev.entity_type = 'task'
                 AND ev.action IN ('cancelled', 'escalated_to_operator'))
             OR (ev.entity_type = 'github' AND ev.action = 'pr_merged')
            )
      )
""")
"""SQL for the sweep — kept module-level so the test suite can assert it
runs and so the query is reviewable in one place.

Why the CTEs:

  * ``step_events`` normalizes the raw event rows, extracting step_index
    as an int and other fields from the polymorphic payload.
  * ``latest_step_event`` finds the latest event per (task, step_index).
    If that event is ``step.ready``, the step is stalled (no ``step.started``
    came after it; if it had, that would be the latest event for that step).

The ``WHERE lse.step_action = 'ready'`` filters to the stalled steps.
The final ``NOT EXISTS`` clause makes the sweep idempotent at the SQL
layer: any task already escalated (or terminal via cancellation / merge)
is excluded, so re-running the sweep on the next tick does not produce a
duplicate event.
"""


async def run_step_starvation_sweep(
    session: AsyncSession,
    dispatcher: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Detect stalled steps (ready but never started) and escalate each to operator.

    Returns the count of escalations emitted on this tick. ``now`` is
    overridable for tests; production callers pass ``None`` and the
    sweep clocks itself.

    The sweep is a pure read followed by a series of single-event
    INSERTs via ``_emit_operator_escalation``. No new ``WorkflowRun`` is
    materialized — this bot is a deterministic detector wired straight
    onto the scheduled tick, not a role-step workflow.
    """
    # Local import — ``triggers`` imports this module via ``handle_scheduled_tick``
    # so a top-level import would be circular.
    from treadmill_api.coordination.triggers import _emit_operator_escalation

    moment = now if now is not None else datetime.now(timezone.utc)
    cutoff = moment - STEP_STARVATION_THRESHOLD

    result = await session.execute(_STARVATION_STEPS_SQL, {"cutoff": cutoff})
    rows = list(result)

    if not rows:
        logger.debug(
            "step-starvation sweep: no stalled steps at %s",
            moment.isoformat(),
        )
        return 0

    escalated = 0
    for row in rows:
        try:
            seconds_since_ready = (moment - row.ready_at).total_seconds()
            await _emit_operator_escalation(
                session,
                dispatcher,
                task_id=row.task_id,
                repo=row.repo,
                signal=STEP_STARVATION_SIGNAL,
                detail=(
                    f"step '{row.step_name}' (role: {row.role_id}) ready at "
                    f"{row.ready_at.isoformat()} but never started; "
                    f"{seconds_since_ready:.0f} seconds stalled. "
                    f"Operator intervention needed."
                ),
                reason="step_starvation",
            )
            escalated += 1
        except Exception:
            logger.exception(
                "step-starvation sweep: escalation failed for task %s; "
                "continuing",
                row.task_id,
            )

    logger.info(
        "step-starvation sweep: escalated %d/%d stalled step(s)",
        escalated, len(rows),
    )
    return escalated
