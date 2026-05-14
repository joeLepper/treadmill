"""Re-evaluation pass for the coordination consumer (D.6).

When a lifecycle event lands that *might* unblock a downstream task —
``step.completed``, ``step.failed`` (eventually), ``plan.activated``, or
``github.pr_merged`` — the consumer re-scans for two populations:

  1. **Never-dispatched tasks** — ``task_status.derived_status =
     'registered'``, belonging to an ``active`` plan, with no workflow
     runs yet.
  2. **Deferred-run tasks** — tasks that *do* have a ``workflow_runs``
     row (so ``task_status`` projects them as ``<wf>: executing``) but
     have not yet had a ``step.ready`` event emitted. These are the
     leftover artifact of the dispatcher's D.2 / D.5 deferred-dispatch
     path: a prior dispatch call hit a gate, persisted the run + step
     rows so the run graph stays complete, but skipped publish. When a
     satisfying event lands later (the canonical case: ``pr_merged``
     satisfies a ``task.<uuid>.pr_merged`` dependency) the re-evaluation
     pass must re-call ``dispatch_task`` on these so step.ready actually
     fires.

Hole 4 (2026-05-13 handoff) — without population (2), tasks with deferred
runs sit forever: ``reevaluate`` filtered them out (they aren't
``registered`` anymore) and so the dispatcher was never re-called even
when their dependency was satisfied. See
``docs/handoffs/2026-05-13-ralph-loop-scoping-signal.md`` for the bug
class. The dispatcher's ``_find_deferred_run`` helper (added with this
fix) ensures the re-call reuses the existing run rather than stacking a
duplicate.

The dispatcher itself remains the one place that gates on
``task_dependencies`` / ``plan_status`` — this module just finds
candidate tasks and calls ``Dispatcher.dispatch_task``; the dispatcher
skips publish if any of its gates still fire (Phase 3 D.2 + D.5 own
those gates), and the deferred run is kept around for the next pass.

Idempotency: a task whose step.ready event has been emitted hits the
dispatcher's idempotency probe (``_has_step_ready_event``) and
short-circuits cleanly. Repeated deliveries of the same trigger event
produce at most one ``step.ready``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models import Task

logger = logging.getLogger("treadmill.coordination.redispatch")


_PENDING_TASKS_SQL = sa.text(
    """
    SELECT t.id
    FROM tasks t
    JOIN plan_status ps ON ps.id = t.plan_id
    LEFT JOIN task_status ts ON ts.id = t.id
    WHERE ps.derived_status = 'active'
      AND (
            ts.derived_status = 'registered'
         OR (
                EXISTS (
                    SELECT 1 FROM workflow_runs r WHERE r.task_id = t.id
                )
            AND NOT EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.task_id = t.id
                      AND e.entity_type = 'step'
                      AND e.action = 'ready'
                )
            )
          )
    """
)
"""Tasks that may now be dispatchable.

Two populations:

  * ``task_status.derived_status = 'registered'`` — never dispatched
    (the original D.6 set).
  * Has a ``workflow_runs`` row but no ``step.ready`` event — the
    deferred-dispatch case (hole 4 from the 2026-05-13 handoff). The
    dispatcher's ``_find_deferred_run`` reuses the existing run when
    re-called, so picking these up here is safe — no duplicate run
    rows result.

The dispatcher enforces dependency + plan-active gates as the final
authority (Phase 3 D.2 + D.5); this SELECT errs on the side of
including candidates that may still be gated.
"""


async def reevaluate(
    session: AsyncSession,
    dispatcher: Any,
) -> list[uuid.UUID]:
    """Find and dispatch every newly-eligible task.

    Returns the list of dispatched task ids. The caller is responsible
    for committing — every ``dispatch_task`` call flushes but does not
    commit, matching the dispatcher's contract in the HTTP path.

    Returns an empty list when ``dispatcher`` is ``None`` (the consumer
    was constructed without one — typical for narrow unit tests).
    """
    if dispatcher is None:
        return []

    result = await session.execute(_PENDING_TASKS_SQL)
    task_ids = [row.id for row in result.all()]
    if not task_ids:
        return []

    dispatched: list[uuid.UUID] = []
    for task_id in task_ids:
        task = await session.get(Task, task_id)
        if task is None:
            continue
        try:
            await dispatcher.dispatch_task(session, task)
        except Exception:
            # An individual dispatch failure must not stop the others.
            # The dispatcher already logs internally; we mirror the
            # router pattern of swallowing here so one bad task doesn't
            # poison the whole re-evaluation pass.
            logger.exception(
                "redispatch: dispatch_task failed for task_id=%s; continuing",
                task_id,
            )
            continue
        dispatched.append(task_id)
    return dispatched
