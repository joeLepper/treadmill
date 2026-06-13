"""``/api/v1/task_executions`` — ADR-0087 per-dispatch lifecycle surface.

Four endpoints:

* ``POST /api/v1/task_executions`` — coordinator writes one row at
  dispatch time; returns 201 + row.
* ``PATCH /api/v1/task_executions/{id}`` — coordinator updates status
  (running→completed or running→failed) and optionally sets
  ``completed_at`` / ``failure_reason``. Only fields present in the
  body are written (``model_fields_set`` semantics, same as
  ``/api/v1/workflow_run_steps``).
* ``GET /api/v1/task_executions?task_id=<id>[&status=<status>]`` —
  read executions for a task or worker label, optionally filtered by
  ``status`` (running/completed/failed). Used by the evaluator, the
  coordinator stale-sweep (?status=running narrows to in-flight rows
  only), and the ADR-0089 token harvester (worker_label filter).
* ``POST /api/v1/task_executions/reconcile-coordinator-restart`` —
  restores rows mis-marked ``failed/coordinator_restart`` on tasks that
  are now in a terminal good state (``pr_merged``, ``done``,
  ``cancelled``). Idempotent; coordinator calls on restart after the
  stale-sweep.

404 when the referenced ``task_id`` or execution ``id`` does not exist.
409 when the ``uq_task_executions_spawn`` UNIQUE constraint fires (same
``(task_id, trigger, worker_label, started_at)`` already exists) — the
coordinator receives a clean signal to short-circuit rather than retry.
422 when ``trigger`` or ``status`` values are out-of-vocabulary (Pydantic
``field_validator`` raises ``ValueError`` → FastAPI 422 Unprocessable Entity).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
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
    try:
        await session.flush()
        await session.refresh(execution)
        await session.commit()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"task_execution already exists for "
                f"(task_id={body.task_id!s}, trigger={body.trigger!r}, "
                f"worker_label={body.worker_label!r}, started_at=<server-assigned>); "
                "coordinator may have restarted mid-spawn"
            ),
        )
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
    """Partial-update a task_execution.

    ``trigger`` is intentionally not patchable — it records the dispatch
    event that created the row and must not change after creation.
    """
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


_RECONCILE_COORDINATOR_RESTART_SQL = """\
UPDATE task_executions
   SET status        = 'completed',
       failure_reason = NULL
 WHERE status         = 'failed'
   AND failure_reason = 'coordinator_restart'
   AND task_id IN (
       SELECT id FROM task_status
        WHERE derived_status IN ('pr_merged', 'done', 'cancelled')
   )
"""


@router.get(
    "/task_executions",
    response_model=list[TaskExecutionRow],
)
async def list_task_executions(
    session: Annotated[AsyncSession, Depends(get_session)],
    task_id: uuid.UUID | None = None,
    worker_label: str | None = None,
    execution_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[TaskExecutionRow]:
    """List executions by task or by worker label, with optional status filter.

    ``worker_label`` serves the ADR-0089 token harvester's window join
    (label + started_at..completed_at → call attribution). At least one
    of ``task_id`` / ``worker_label`` is required — the unfiltered table
    is unbounded. ``?status=running`` narrows to in-flight rows only,
    which is what the coordinator's startup stale-sweep must use to avoid
    clobbering already-terminal executions.
    """
    if task_id is None and worker_label is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provide at least one of task_id, worker_label",
        )
    if execution_status is not None and execution_status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"status must be one of {sorted(_VALID_STATUSES)!r}; "
                f"got {execution_status!r}"
            ),
        )
    query = select(TaskExecution).order_by(TaskExecution.started_at)
    if task_id is not None:
        query = query.where(TaskExecution.task_id == task_id)
    if worker_label is not None:
        query = query.where(TaskExecution.worker_label == worker_label)
    if execution_status is not None:
        query = query.where(TaskExecution.status == execution_status)
    result = await session.execute(query)
    rows = result.scalars().all()
    return [TaskExecutionRow.model_validate(r, from_attributes=True) for r in rows]


class ReconcileCoordinatorRestartResponse(BaseModel):
    reconciled: int


@router.post(
    "/task_executions/reconcile-coordinator-restart",
    response_model=ReconcileCoordinatorRestartResponse,
)
async def reconcile_coordinator_restart_executions(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReconcileCoordinatorRestartResponse:
    """Restore execution rows wrongly marked failed/coordinator_restart.

    When the coordinator's startup stale-sweep lacked a ``?status=running``
    filter, it marked ALL historical execution rows (including already-terminal
    ones) as ``failed/coordinator_restart``. This endpoint restores rows on
    tasks now in a terminal good state (``pr_merged``, ``done``, ``cancelled``)
    back to ``completed``.

    Idempotent — calling multiple times is safe; already-restored rows no
    longer match the WHERE predicate.
    """
    result = await session.execute(text(_RECONCILE_COORDINATOR_RESTART_SQL))
    await session.commit()
    return ReconcileCoordinatorRestartResponse(reconciled=result.rowcount)
