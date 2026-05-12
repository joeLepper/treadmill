"""Re-evaluation pass for the coordination consumer (D.6).

When a lifecycle event lands that *might* unblock a downstream task —
``step.completed``, ``step.failed`` (eventually), ``plan.activated``, or
later ``github.pr_merged`` — the consumer re-scans for tasks that:

  1. Have ``task_status.derived_status = 'registered'`` (not blocked,
     not yet dispatched).
  2. Belong to a plan whose ``plan_status.derived_status = 'active'``.
  3. Have no ``step.ready`` event yet for their run (proxy: no run rows).

and dispatches each of them. The dispatcher itself is the one place that
gates on ``task_dependencies`` / ``plan_status`` — this module just
finds candidate tasks and calls ``Dispatcher.dispatch_task``; the
dispatcher skips if any of its gates fire (Phase 3 D.2 + D.5 own those
gates).

At v0 only ``step.completed`` and ``plan.activated`` trigger this pass.
Other event types (``github.pr_merged``, ``step.failed``) land in Week 3
with the trigger evaluator (per the 2026-05-11 closure plan).

Idempotency: a task that was already dispatched in a prior pass has a
``workflow_runs`` row — its ``task_status.derived_status`` is no longer
``'registered'`` and the SELECT below filters it out. So repeated
deliveries of the same trigger event produce at most one dispatch.
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
    JOIN task_status ts ON ts.id = t.id
    JOIN plan_status ps ON ps.id = t.plan_id
    WHERE ts.derived_status = 'registered'
      AND ps.derived_status = 'active'
    """
)
"""Tasks that may now be dispatchable.

The ``task_status`` VIEW already collapses to ``'registered'`` only for
tasks that have no workflow runs *and* no unmet dependencies — see the
0002 migration. So this SELECT is sufficient to find candidates; the
dispatcher then enforces dependency + plan-active gates as the final
authority (Phase 3 D.2 + D.5).
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
