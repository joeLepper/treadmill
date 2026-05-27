"""``POST /api/v1/dashboard/tasks/{task_id}/ack-escalation`` — operator ack.

Backs the dashboard's ``useAcknowledgeEscalation`` mutation (mock at
``services/dashboard/src/api/mock.ts`` ``acknowledgeEscalation``):
inserts a single ``task.escalation_acknowledged`` event for the task,
which the overview escalation query
(``routers/dashboard/overview.py`` ``_ESCALATIONS_SQL``) reads to filter
the task back out of the escalation list.

Pairs with ``task.escalated_to_operator`` emitted by
``coordination/stuck_task_sweep.py``. Status codes:

  * ``202`` — fresh ack written.
  * ``200`` — most recent escalation already acked (idempotent no-op);
    returns the existing event id.
  * ``404`` — task does not exist.
  * ``409`` — task has no outstanding escalation.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session


router = APIRouter()


class AckEscalationResponse(BaseModel):
    event_id: str
    task_id: str


_TASK_EXISTS_SQL = text("SELECT 1 FROM tasks WHERE id = :task_id")


# Most-recent escalation event and most-recent ack event for a task, side
# by side in one round-trip. Mirrors the structure of overview's
# ``_ESCALATIONS_SQL`` (last-of-each via DISTINCT ON), so the predicate
# the dashboard reads in the GET path and the predicate we write through
# here stay synchronized.
_LAST_ESCALATION_AND_ACK_SQL = text("""
    WITH last_escalation AS (
        SELECT id, created_at
        FROM events
        WHERE task_id = :task_id
          AND entity_type = 'task'
          AND action = 'escalated_to_operator'
        ORDER BY created_at DESC
        LIMIT 1
    ),
    last_ack AS (
        SELECT id, created_at
        FROM events
        WHERE task_id = :task_id
          AND entity_type = 'task'
          AND action = 'escalation_acknowledged'
        ORDER BY created_at DESC
        LIMIT 1
    )
    SELECT
        (SELECT id         FROM last_escalation) AS escalation_id,
        (SELECT created_at FROM last_escalation) AS escalation_at,
        (SELECT id         FROM last_ack)        AS ack_id,
        (SELECT created_at FROM last_ack)        AS ack_at
""")


_INSERT_ACK_SQL = text("""
    INSERT INTO events (entity_type, action, task_id, payload)
    VALUES ('task', 'escalation_acknowledged', :task_id, '{}'::jsonb)
    RETURNING id
""")


@router.post(
    "/tasks/{task_id}/ack-escalation",
    response_model=AckEscalationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ack_escalation(
    task_id: uuid.UUID,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AckEscalationResponse:
    """Acknowledge the outstanding operator escalation for ``task_id``.

    Body is intentionally empty — the act of acknowledging is the
    payload; reason/operator-identity would be additive over a later
    iteration if needed.
    """
    exists = (
        await session.execute(_TASK_EXISTS_SQL, {"task_id": task_id})
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )

    row = (
        await session.execute(
            _LAST_ESCALATION_AND_ACK_SQL, {"task_id": task_id},
        )
    ).one()

    if row.escalation_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task is not currently escalated",
        )

    if row.ack_at is not None and row.ack_at >= row.escalation_at:
        # Already acked since the latest escalation — surface the
        # existing event row so the client treats the call as a no-op.
        response.status_code = status.HTTP_200_OK
        return AckEscalationResponse(
            event_id=str(row.ack_id), task_id=str(task_id),
        )

    new_event_id = (
        await session.execute(_INSERT_ACK_SQL, {"task_id": task_id})
    ).scalar_one()
    await session.commit()
    return AckEscalationResponse(
        event_id=str(new_event_id), task_id=str(task_id),
    )
