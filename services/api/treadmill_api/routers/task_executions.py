"""``/api/v1/task_executions`` — ADR-0087 per-dispatch lifecycle surface.

Three endpoints:

* ``POST /api/v1/task_executions`` — coordinator writes one row at
  dispatch time; returns 201 + row.
* ``PATCH /api/v1/task_executions/{id}`` — coordinator updates status
  (running→completed or running→failed) and optionally sets
  ``completed_at`` / ``failure_reason``. Only fields present in the
  body are written (``model_fields_set`` semantics, same as
  ``/api/v1/workflow_run_steps``).
* ``GET /api/v1/task_executions?task_id=<id>`` — read all executions
  for a task, ordered by ``started_at``. Used by the evaluator and
  by the coordinator to count rework cycles.

404 when the referenced ``task_id`` or execution ``id`` does not exist.
400 when ``trigger`` or ``status`` values are out-of-vocabulary.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Task, TaskExecution


router = APIRouter(prefix="/api/v1", tags=["task_executions"])

_VALID_TRIGGERS = frozenset(
    {"initial", "coordinator-rework", "evaluator-rework", "peer-review"}
)
_VALID_STATUSES = frozenset({"running", "completed", "failed"})


# ── Pydantic schemas ─────────────────────────────────────────────────────


class TaskExecutionCreate(BaseModel):
    task_id: uuid.UUID
    worker_label: str = Field(min_length=1)
    trigger: str

    @field_validator("trigger")
    @classmethod
    def validate_trigger(cls, v: str) -> str:
        if v not in _VALID_TRIGGERS:
            raise ValueError(
                f"trigger must be one of {sorted(_VALID_TRIGGERS)!r}; got {v!r}"
            )
        return v


class TaskExecutionUpdate(BaseModel):
    status: str | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_VALID_STATUSES)!r}; got {v!r}"
            )
        return v


class TaskExecutionRow(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    worker_label: str
    trigger: str
    status: str
    failure_reason: str | None
    started_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post(
    "/task_executions",
    response_model=TaskExecutionRow,
    status_code=status.HTTP_201_CREATED,
)
async def create_task_execution(
    body: TaskExecutionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskExecutionRow:
    task = await session.get(Task, body.task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {body.task_id!s} not found",
        )
    execution = TaskExecution(
        task_id=body.task_id,
        worker_label=body.worker_label,
        trigger=body.trigger,
        status="running",
    )
    session.add(execution)
    await session.flush()
    await session.refresh(execution)
    await session.commit()
    return TaskExecutionRow.model_validate(execution, from_attributes=True)


@router.patch(
    "/task_executions/{execution_id}",
    response_model=TaskExecutionRow,
)
async def update_task_execution(
    execution_id: uuid.UUID,
    body: TaskExecutionUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskExecutionRow:
    execution = await session.get(TaskExecution, execution_id)
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task_execution {execution_id!s} not found",
        )
    for field in body.model_fields_set:
        setattr(execution, field, getattr(body, field))
    await session.flush()
    await session.refresh(execution)
    await session.commit()
    return TaskExecutionRow.model_validate(execution, from_attributes=True)


@router.get(
    "/task_executions",
    response_model=list[TaskExecutionRow],
)
async def list_task_executions(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TaskExecutionRow]:
    result = await session.execute(
        select(TaskExecution)
        .where(TaskExecution.task_id == task_id)
        .order_by(TaskExecution.started_at)
    )
    rows = result.scalars().all()
    return [TaskExecutionRow.model_validate(r, from_attributes=True) for r in rows]
