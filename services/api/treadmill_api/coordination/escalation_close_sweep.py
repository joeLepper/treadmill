"""Escalation-close sweep — closes the incident lifecycle (ADR-0062 Step 2).

The ``*/2`` ``wf-escalation-close-sweep`` schedule (seed/schedules.py) fires
this deterministic close-detector on every tick. The sweep mirrors
``stuck_task_sweep`` in shape: a pure query over the event stream + a
series of single-event INSERTs via ``dispatcher.persist_and_publish``.
No new ``WorkflowRun`` is materialized — like the stuck-task sweep, this
bot is wired straight onto the scheduled tick, not a role-step workflow.

The signal — escalations are the start of an *incident* (ADR-0062), and
an incident closes when the underlying task satisfies any of the five
close triggers below — turns the existing "open ticket forever" model
into a paired open/close lifecycle with MTTR observable on every close:

  * ``re_progressed`` — a ``step.completed`` event landed for the task
    with ``created_at > opened_at`` (the task is dispatching again; the
    underlying stall is gone).
  * ``pr_merged`` — a ``github.pr_merged`` event exists for the task
    (the change shipped; whatever was blocking the loop is resolved by
    the merge).
  * ``cancelled`` — a ``task.cancelled`` terminal landed.
  * ``superseded`` — a ``task.superseded`` terminal landed (a replacement
    task took over).
  * ``operator_close`` — emitted via the CLI / API path
    (``emit_operator_close``) NOT via this sweep. Listed here for the
    full close-trigger taxonomy; the sweep skips it because the
    operator-driven close has already fired.

The five triggers are checked in priority order: ``re_progressed`` first
(the most informative — task is healthy again), then the three terminal
triggers. The first match wins; we emit one ``task.escalation_closed``
event per open incident per tick.

Idempotency: the SQL excludes any task whose most recent
``escalated_to_operator`` already has a later ``escalation_closed`` —
re-running the sweep on the next tick does not pile on a second close
event for the same incident.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.events.task import TaskEscalationClosed

logger = logging.getLogger("treadmill.coordination.escalation_close_sweep")


ESCALATION_CLOSE_SWEEP_WORKFLOW_ID = "wf-escalation-close-sweep"
"""Schedule's ``workflow_id`` value. ``handle_scheduled_tick`` intercepts
this slug and runs the deterministic close-detection sweep instead of
looking up a ``WorkflowVersion`` (there is none — this bot is a query,
not a role)."""


CloseReason = Literal[
    "re_progressed",
    "pr_merged",
    "cancelled",
    "superseded",
    "operator_close",
]


_OPEN_INCIDENTS_SQL = text("""
    WITH latest_open AS (
        SELECT DISTINCT ON (task_id)
            task_id, id AS opened_event_id, created_at AS opened_at
        FROM events
        WHERE task_id IS NOT NULL
          AND entity_type = 'task'
          AND action = 'escalated_to_operator'
        ORDER BY task_id, created_at DESC
    )
    SELECT lo.task_id AS task_id,
           lo.opened_at AS opened_at
    FROM latest_open lo
    WHERE NOT EXISTS (
        SELECT 1 FROM events ev
        WHERE ev.task_id = lo.task_id
          AND ev.entity_type = 'task'
          AND ev.action = 'escalation_closed'
          AND ev.created_at > lo.opened_at
    )
""")
"""Find every open incident: the most recent ``escalated_to_operator``
per task that has no later ``escalation_closed`` for the same task.

``DISTINCT ON`` picks the latest open event per task; the ``NOT EXISTS``
clause makes the sweep idempotent at the SQL layer — once a close event
lands on the next tick, the same incident no longer appears.
"""


_RE_PROGRESSED_SQL = text("""
    SELECT 1 FROM events
    WHERE task_id = :task_id
      AND entity_type = 'step'
      AND action = 'completed'
      AND created_at > :opened_at
    LIMIT 1
""")
"""``re_progressed`` close trigger: any ``step.completed`` after the
incident opened means the task is dispatching again — the stall that
caused the escalation is gone."""


_PR_MERGED_SQL = text("""
    SELECT 1 FROM events
    WHERE task_id = :task_id
      AND entity_type = 'github'
      AND action = 'pr_merged'
    LIMIT 1
""")
"""``pr_merged`` close trigger: a ``github.pr_merged`` event for the
task. We don't require ``created_at > opened_at`` because a merged PR
makes the incident moot regardless of when the merge landed (an
escalation against an already-merged task closes on the next sweep)."""


_TASK_CANCELLED_SQL = text("""
    SELECT 1 FROM events
    WHERE task_id = :task_id
      AND entity_type = 'task'
      AND action = 'cancelled'
    LIMIT 1
""")
"""``cancelled`` close trigger: the task hit its terminal cancellation
verb."""


_TASK_SUPERSEDED_SQL = text("""
    SELECT 1 FROM events
    WHERE task_id = :task_id
      AND entity_type = 'task'
      AND action = 'superseded'
    LIMIT 1
""")
"""``superseded`` close trigger: a replacement task took over (ADR-0048
supersede flow)."""


# Priority order for the close-trigger checks. ``re_progressed`` first
# because it carries the most operational information (the task is
# healthy again); the three terminal triggers follow. ``operator_close``
# is NOT checked here — that path emits its own close event via
# ``emit_operator_close`` and the sweep skips it because the close has
# already been written.
_CLOSE_TRIGGER_CHECKS: list[tuple[CloseReason, Any]] = [
    ("re_progressed", _RE_PROGRESSED_SQL),
    ("pr_merged", _PR_MERGED_SQL),
    ("cancelled", _TASK_CANCELLED_SQL),
    ("superseded", _TASK_SUPERSEDED_SQL),
]


async def _detect_close_reason(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    opened_at: datetime,
) -> CloseReason | None:
    """Check the four sweep-detectable close triggers in priority order.

    Returns the first matching reason, or ``None`` when the incident is
    still open (no trigger fires). ``re_progressed`` uses the
    ``opened_at`` cutoff; the three terminal triggers don't, because a
    terminal verb after the open is structurally impossible to
    re-distinguish in a meaningful way (cancellation is cancellation).
    """
    for reason, sql in _CLOSE_TRIGGER_CHECKS:
        params: dict[str, Any] = {"task_id": task_id}
        if reason == "re_progressed":
            params["opened_at"] = opened_at
        result = await session.execute(sql, params)
        if result.first() is not None:
            return reason
    return None


async def emit_escalation_closed(
    session: AsyncSession,
    dispatcher: Any,
    *,
    task_id: uuid.UUID,
    opened_at: datetime,
    close_reason: CloseReason,
    now: datetime | None = None,
    expected_followup: str | None = None,
) -> None:
    """Persist + publish one ``task.escalation_closed`` event.

    Shared between the sweep (four trigger reasons) and the CLI
    ``treadmill escalations close`` path (Step 3 — ``operator_close``).
    ``mttr_seconds`` is computed at emit-time as
    ``(now - opened_at).seconds`` so the value is stamped on the event
    payload and consumers don't need to re-derive it.

    No-op when the dispatcher is absent (test stubs) or the task row is
    missing — the caller's warning log already recorded the close.
    """
    # Local import — ``triggers`` already imports models lazily for the
    # same circular-import reason; we follow the precedent.
    from treadmill_api.models import Task

    if dispatcher is None:
        return
    task = await session.get(Task, task_id)
    if task is None:
        logger.warning(
            "escalation-close: task %s not found; skipping %s close",
            task_id, close_reason,
        )
        return

    moment = now if now is not None else datetime.now(timezone.utc)
    # Datetimes stored in the events table are timezone-aware UTC; the
    # subtraction yields a ``timedelta`` whose ``.seconds`` is the
    # within-day component, so we use ``.total_seconds()`` (cast to int)
    # to capture MTTR for incidents that span days.
    mttr_seconds = int((moment - opened_at).total_seconds())

    try:
        await dispatcher.persist_and_publish(
            session,
            entity_type="task",
            action="escalation_closed",
            payload=TaskEscalationClosed(
                close_reason=close_reason,
                opened_at=opened_at,
                mttr_seconds=mttr_seconds,
                expected_followup=expected_followup,
            ),
            plan_id=task.plan_id,
            task_id=task_id,
        )
    except Exception:
        logger.exception(
            "escalation-close: failed to emit close for task %s (%s); "
            "next sweep tick will retry",
            task_id, close_reason,
        )


async def emit_operator_close(
    session: AsyncSession,
    dispatcher: Any,
    *,
    task_id: uuid.UUID,
    opened_at: datetime,
    now: datetime | None = None,
    expected_followup: str | None = None,
) -> None:
    """Emit ``task.escalation_closed`` with ``close_reason='operator_close'``.

    The CLI ``treadmill escalations close <task_id>`` command (Step 3)
    calls this directly. Lives in this module so the close-emission
    code path is single-sourced — the sweep and the CLI share one
    emitter and ADR-0062's "MTTR computed at close time" invariant is
    enforced in one place.
    """
    await emit_escalation_closed(
        session,
        dispatcher,
        task_id=task_id,
        opened_at=opened_at,
        close_reason="operator_close",
        now=now,
        expected_followup=expected_followup,
    )


async def run_escalation_close_sweep(
    session: AsyncSession,
    dispatcher: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Detect closes against every open incident and emit one close
    event per matched task.

    Returns the count of close events emitted on this tick. ``now`` is
    overridable for tests; production callers pass ``None`` and the
    sweep clocks itself.

    The sweep is a pure read (open-incidents SELECT + per-incident
    close-trigger probes) followed by a series of single-event INSERTs
    via ``emit_escalation_closed``. No ``WorkflowRun`` is materialized
    — this bot is a deterministic detector wired straight onto the
    scheduled tick.
    """
    moment = now if now is not None else datetime.now(timezone.utc)

    result = await session.execute(_OPEN_INCIDENTS_SQL)
    rows = list(result)

    if not rows:
        logger.debug(
            "escalation-close sweep: no open incidents at %s",
            moment.isoformat(),
        )
        return 0

    closed = 0
    for row in rows:
        try:
            reason = await _detect_close_reason(
                session,
                task_id=row.task_id,
                opened_at=row.opened_at,
            )
            if reason is None:
                continue
            await emit_escalation_closed(
                session,
                dispatcher,
                task_id=row.task_id,
                opened_at=row.opened_at,
                close_reason=reason,
                now=moment,
                expected_followup="transient:auto_progress",
            )
            closed += 1
        except Exception:
            logger.exception(
                "escalation-close sweep: close emission failed for task %s; "
                "continuing",
                row.task_id,
            )

    logger.info(
        "escalation-close sweep: closed %d/%d open incident(s)",
        closed, len(rows),
    )
    return closed
