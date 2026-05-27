"""``POST /api/v1/dashboard/tasks/{task_id}/cancel`` — Operator cancel action.

Backs ``services/dashboard/src/api/queries.ts`` ``useCancelTask``. The
body shape ``{reason: str}`` matches the dashboard mutation hook.

Inserts one ``task.cancelled`` event row (``payload = {"reason": <body>}``)
via the shared ``Dispatcher.persist_and_publish`` seam — the same path
``routers/plans.py`` uses for ``task.registered`` — so downstream
consumers see the cancellation through the normal events bus.

Idempotency: if the task already carries a terminal lifecycle event
(``task.cancelled`` / ``task.merged`` / ``task.done``), the route 409s
without inserting a duplicate.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher
from treadmill_api.events.task import TaskCancelled


router = APIRouter()


_TERMINAL_ACTIONS = ("cancelled", "merged", "done")


class CancelTaskRequest(BaseModel):
    reason: str = Field(..., min_length=1)


class CancelTaskResponse(BaseModel):
    event_id: uuid.UUID
    task_id: uuid.UUID


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=CancelTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_task(
    task_id: uuid.UUID,
    body: CancelTaskRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> CancelTaskResponse:
    """Emit ``task.cancelled`` for ``task_id``.

    Returns 404 when the task does not exist, 409 when the task is
    already terminal (a prior ``task.cancelled`` / ``task.merged`` /
    ``task.done`` event lives on the row), and 422 when ``reason`` is
    missing or empty (enforced by Pydantic).
    """
    exists = (
        await session.execute(
            text("SELECT 1 FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        )
    ).first()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id} not found",
        )

    terminal = (
        await session.execute(
            text(
                "SELECT action FROM events "
                "WHERE task_id = :task_id "
                "  AND entity_type = 'task' "
                "  AND action = ANY(:terminal_actions) "
                "ORDER BY created_at DESC "
                "LIMIT 1"
            ),
            {
                "task_id": task_id,
                "terminal_actions": list(_TERMINAL_ACTIONS),
            },
        )
    ).first()
    if terminal is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"task {task_id} is already terminal "
                f"(action={terminal.action!r}); not inserting a second "
                "task.cancelled event"
            ),
        )

    event = await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="cancelled",
        payload=TaskCancelled(reason=body.reason),
        task_id=task_id,
    )
    await session.commit()
    return CancelTaskResponse(event_id=event.id, task_id=task_id)
