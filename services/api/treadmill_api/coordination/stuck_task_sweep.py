"""Deterministic stuck-task sweep — first real self-health bot (ADR-0035 P2).

The ``*/10`` ``wf-stuck-task-sweep`` schedule (seed/schedules.py) fires this
deterministic detector on every tick. The signal — empirically validated by
the 2026-05-14 parser silent-stall (task ``0ac62421``) — is:

  * the latest ``step`` event for the task is ``step.completed`` with no
    later ``step.ready`` (no downstream step was dispatched),
  * the task's most recent activity (any event) is older than
    ``STUCK_TASK_THRESHOLD``,
  * the task is not already terminal (no ``task.cancelled``, no
    ``github.pr_merged``, no prior ``task.escalated_to_operator``).

Per ADR-0047, detection is a query — no LLM judgment needed. The sweep
emits ``task.escalated_to_operator`` for each stalled task it finds,
re-using the existing ``_emit_operator_escalation`` path so the
``GET /api/v1/tasks?derived_status=needs_operator`` surface (ADR-0048 §3)
picks it up. Idempotency is enforced two ways: the SQL excludes any task
that already has an ``escalated_to_operator`` event, and the emitter
itself dedups on ``(task_id, signal)``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("treadmill.coordination.stuck_task_sweep")


STUCK_TASK_SWEEP_WORKFLOW_ID = "wf-stuck-task-sweep"
"""Schedule's ``workflow_id`` value. ``handle_scheduled_tick`` intercepts
this slug and runs the deterministic sweep instead of looking up a
``WorkflowVersion`` (there is none — this bot is a query, not a role)."""


STUCK_TASK_THRESHOLD = timedelta(minutes=30)
"""How long a task can sit with a completed-but-undispatched terminal step
before the sweep escalates it. Tuned against the empirical hours-long
parser stall (2026-05-14); short enough that operator intervention is
prompt, long enough that legitimate analyzer→action gaps (~minutes) don't
trip the detector."""


STUCK_TASK_SIGNAL = "wf-stuck-task-sweep-stalled"
"""The ``last_verdict`` stamped on the escalation event. The generic
emitter's ``(task_id, signal)`` dedup uses this as the key — a second
sweep tick on the same stall reads the existing row and no-ops."""


_STUCK_TASKS_SQL = text("""
    WITH latest_step_event AS (
        SELECT DISTINCT ON (task_id)
            task_id, action AS step_action, created_at AS last_step_at
        FROM events
        WHERE task_id IS NOT NULL AND entity_type = 'step'
        ORDER BY task_id, created_at DESC
    ),
    latest_any_event AS (
        SELECT task_id, MAX(created_at) AS last_activity
        FROM events
        WHERE task_id IS NOT NULL
        GROUP BY task_id
    )
    SELECT t.id AS task_id,
           t.repo AS repo,
           lse.last_step_at AS last_step_at,
           lae.last_activity AS last_activity
    FROM tasks t
    JOIN latest_step_event lse ON lse.task_id = t.id
    JOIN latest_any_event lae ON lae.task_id = t.id
    WHERE lse.step_action = 'completed'
      AND lae.last_activity < :cutoff
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

Why the two CTEs:

  * ``latest_step_event`` finds the latest ``step.*`` event per task. If
    that event is ``step.completed``, no later ``step.ready`` exists,
    which is the precise "no downstream step dispatched" signal.
  * ``latest_any_event`` finds the absolute last activity per task —
    catches the case where a webhook landed after the step completion
    but no follow-up was triggered. Without this guard a noisy github
    event would mask the stall.

The ``NOT EXISTS`` clause makes the sweep idempotent at the SQL layer:
any task already escalated (or terminal via cancellation / merge) is
excluded, so re-running the sweep on the next tick does not produce a
duplicate event.
"""


async def run_stuck_task_sweep(
    session: AsyncSession,
    dispatcher: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Detect silently-stalled tasks and escalate each one to operator.

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
    cutoff = moment - STUCK_TASK_THRESHOLD

    result = await session.execute(_STUCK_TASKS_SQL, {"cutoff": cutoff})
    rows = list(result)

    if not rows:
        logger.debug("stuck-task sweep: no stalled tasks at %s", moment.isoformat())
        return 0

    escalated = 0
    for row in rows:
        try:
            await _emit_operator_escalation(
                session,
                dispatcher,
                task_id=row.task_id,
                repo=row.repo,
                signal=STUCK_TASK_SIGNAL,
                detail=(
                    f"task last activity at {row.last_activity.isoformat()} "
                    f"(latest step.completed at {row.last_step_at.isoformat()}); "
                    f"no downstream step dispatched within "
                    f"{STUCK_TASK_THRESHOLD}. Operator intervention needed."
                ),
                reason="stuck_task_sweep",  # ADR-0058
            )
            escalated += 1
        except Exception:
            logger.exception(
                "stuck-task sweep: escalation failed for task %s; continuing",
                row.task_id,
            )

    logger.info(
        "stuck-task sweep: escalated %d/%d stalled task(s)",
        escalated, len(rows),
    )
    return escalated
