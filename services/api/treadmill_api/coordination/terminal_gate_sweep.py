"""Deterministic terminal-gate orphan sweep (ADR-0047, ADR-0038, ADR-0042).

The ``*/10`` ``wf-terminal-gate-sweep`` schedule fires this detector on
every tick. The signal — the inverse of the stuck-task sweep — is:

  * the task has an architect accept-as-is verdict: a ``review.override``
    (ADR-0038) or ``validate.override`` (ADR-0042) event in the event stream,
  * the PR is not yet merged: no ``github.pr_merged`` event for the task,
  * the task is not legitimately terminal without a merge: no
    ``task.cancelled`` or ``task.superseded`` event (those verbs explicitly
    leave a PR unmerged — the operator chose to abandon the PR, not ship it).

Per ADR-0047, detection is a query — no LLM judgment needed. The sweep
emits ``task.escalated_to_operator`` for each orphaned PR it finds,
re-using the existing ``_emit_operator_escalation`` path so the
``GET /api/v1/tasks?derived_status=needs_operator`` surface (ADR-0048 §3)
picks it up. Idempotency is enforced two ways: the SQL excludes any task
that already has an ``escalated_to_operator`` event for this specific
signal (signal-specific so prior stuck-task escalations don't mask a
new orphan escalation for the same task), and the emitter itself dedups
on ``(task_id, signal)``.

The ``escalation_close_sweep`` (ADR-0062) auto-closes the escalation once
the PR merges via the ``pr_merged`` close trigger — no manual close needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("treadmill.coordination.terminal_gate_sweep")


TERMINAL_GATE_SWEEP_WORKFLOW_ID = "wf-terminal-gate-sweep"
"""Schedule's ``workflow_id`` value. ``handle_scheduled_tick`` intercepts
this slug and runs the deterministic sweep instead of looking up a
``WorkflowVersion`` (there is none — this bot is a query, not a role)."""


TERMINAL_GATE_SIGNAL = "wf-terminal-gate-sweep-orphaned-pr"
"""The ``last_verdict`` stamped on the escalation event. The generic
emitter's ``(task_id, signal)`` dedup uses this as the key — a second
sweep tick on the same orphan reads the existing row and no-ops."""


_ORPHANED_PR_SQL = text("""
    WITH accepted_tasks AS (
        SELECT DISTINCT ON (task_id)
            task_id,
            CASE entity_type
                WHEN 'review'   THEN 'review.override'
                ELSE                 'validate.override'
            END AS override_verb
        FROM events
        WHERE task_id IS NOT NULL
          AND entity_type IN ('review', 'validate')
          AND action = 'override'
        ORDER BY task_id, created_at DESC
    )
    SELECT t.id            AS task_id,
           t.repo          AS repo,
           at.override_verb AS override_verb,
           tp.pr_number    AS pr_number
    FROM tasks t
    JOIN accepted_tasks at ON at.task_id = t.id
    LEFT JOIN task_prs tp ON tp.task_id = t.id
    WHERE NOT EXISTS (
        SELECT 1 FROM events ev
        WHERE ev.task_id = t.id
          AND ev.entity_type = 'github'
          AND ev.action = 'pr_merged'
    )
    AND NOT EXISTS (
        SELECT 1 FROM events ev
        WHERE ev.task_id = t.id
          AND ev.entity_type = 'task'
          AND ev.action IN ('cancelled', 'superseded')
    )
    AND NOT EXISTS (
        SELECT 1 FROM events ev
        WHERE ev.task_id = t.id
          AND ev.entity_type = 'task'
          AND ev.action = 'escalated_to_operator'
          AND ev.payload->>'last_verdict' = :signal
    )
""")
"""SQL for the sweep — kept module-level so the test suite can assert it
runs and so the query is reviewable in one place.

Why each CTE / clause:

  * ``accepted_tasks`` finds tasks that received an architect accept-as-is
    verdict via ``review.override`` (ADR-0038) or ``validate.override``
    (ADR-0042). ``DISTINCT ON`` picks the latest override event per task
    so ``override_verb`` reflects the most recent accept-as-is action;
    tasks with both override types appear once.
  * The first ``NOT EXISTS`` (``github.pr_merged``) is the orphan signal:
    the PR was accepted by the architect but never merged. The
    escalation-close sweep (ADR-0062) auto-closes this escalation once
    ``github.pr_merged`` arrives — no manual close needed.
  * The second ``NOT EXISTS`` (``task.cancelled`` / ``task.superseded``)
    guards against false positives: these terminal verbs explicitly leave
    a PR unmerged because the operator chose to abandon the work rather
    than ship it. ``cancelled`` and ``superseded`` are legitimate exits
    from the accept-as-is path and MUST NOT be flagged.
  * The third ``NOT EXISTS`` makes the sweep idempotent at the SQL layer:
    signal-specific (``payload->>'last_verdict' = :signal``) so a prior
    stuck-task escalation for the same task does not mask a new orphan
    escalation. Re-running the sweep on the next tick produces no duplicate.
"""


async def run_terminal_gate_sweep(
    session: AsyncSession,
    dispatcher: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Detect architect-accepted but unmerged PRs and escalate each to operator.

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

    result = await session.execute(_ORPHANED_PR_SQL, {"signal": TERMINAL_GATE_SIGNAL})
    rows = list(result)

    if not rows:
        logger.debug(
            "terminal-gate sweep: no orphaned PRs at %s", moment.isoformat()
        )
        return 0

    escalated = 0
    for row in rows:
        try:
            pr_part = (
                f"PR#{row.pr_number}"
                if row.pr_number is not None
                else "PR (number unknown)"
            )
            await _emit_operator_escalation(
                session,
                dispatcher,
                task_id=row.task_id,
                repo=row.repo,
                signal=TERMINAL_GATE_SIGNAL,
                detail=(
                    f"{pr_part} accepted-as-is by architect "
                    f"({row.override_verb}) but not yet merged. "
                    f"Operator review needed."
                ),
                reason="terminal_gate_sweep",
            )
            escalated += 1
        except Exception:
            logger.exception(
                "terminal-gate sweep: escalation failed for task %s; continuing",
                row.task_id,
            )

    logger.info(
        "terminal-gate sweep: escalated %d/%d orphaned PR(s)",
        escalated, len(rows),
    )
    return escalated
