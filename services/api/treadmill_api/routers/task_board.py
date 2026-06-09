"""``/api/v1/task_board`` — coordinator's team-coordination overlay
(ADR-0084 §6 / Task 1C).

Two endpoints — the minimal surface a coordinator needs to maintain its
overlay against the event-derived ``task_status`` view:

  * ``GET  /api/v1/task_board/{plan_id}`` — all board rows for a plan.
    Coordinator reads this on startup as part of reconciliation:
    it compares the board's status (overlay) against the event-derived
    ``task_status`` view (ground truth) and upserts where the two
    diverge.

  * ``PATCH /api/v1/task_board/{task_id}`` — write status, assignee,
    branch, pr_number, notes for a single task. Fields not provided
    are left untouched. Idempotent — re-sending the same payload is a
    no-op-with-updated_at-refresh.

Status vocabulary is validated against ``TASK_BOARD_STATUSES`` in the
model module. Unknown statuses return HTTP 422 — vocab evolution is
a model-file change, not a router change.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.coordination.coordinator_overlay import (
    invalidate_overlay_cache,
)
from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Task
from treadmill_api.models.task_board import TASK_BOARD_STATUSES, TaskBoard


router = APIRouter(prefix="/api/v1/task_board", tags=["task_board"])


class TaskBoardRow(BaseModel):
    """Wire representation of one ``task_board`` row."""

    task_id: uuid.UUID
    plan_id: uuid.UUID
    assignee: str | None = None
    status: str
    branch: str | None = None
    pr_number: int | None = None
    notes: str | None = None
    updated_at: datetime
    updated_by: str | None = None


class TaskBoardPatchRequest(BaseModel):
    """Partial update. Only fields whose value is provided are written.

    None vs. omitted: the JSON body distinguishes ``"assignee": null``
    (explicit clear) from omitting the key. Pydantic's
    ``model_fields_set`` (used in the handler) reads the omitted-vs-null
    distinction so the handler can selectively SET ... = NULL.
    """

    assignee: str | None = None
    status: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    notes: str | None = None
    updated_by: str | None = None
    """Coordinator label that performed this write — recorded on every
    PATCH for audit. Optional but recommended."""

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in TASK_BOARD_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(TASK_BOARD_STATUSES)}; got {v!r}"
            )
        return v


@router.get(
    "/{plan_id}",
    response_model=list[TaskBoardRow],
    summary="List task_board rows for a plan",
)
async def list_task_board_for_plan(
    plan_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TaskBoardRow]:
    """Coordinator reads this on startup to reconcile its overlay against
    the event-derived ``task_status`` view. Returns one row per task on
    the board for ``plan_id``; tasks that haven't been written to the
    board yet are absent (the coordinator inserts them via PATCH after
    reconciliation determines their initial status)."""
    rows = (
        await session.execute(
            select(TaskBoard).where(TaskBoard.plan_id == plan_id).order_by(TaskBoard.updated_at.desc())
        )
    ).scalars().all()
    return [TaskBoardRow.model_validate(r, from_attributes=True) for r in rows]


@router.patch(
    "/{task_id}",
    response_model=TaskBoardRow,
    summary="Upsert a task_board row's coordinator-overlay fields",
)
async def patch_task_board(
    task_id: uuid.UUID,
    body: TaskBoardPatchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskBoardRow:
    """Upsert semantics: if the row doesn't exist yet, insert it with the
    provided fields + the task's plan_id (looked up from the ``tasks``
    table). If it exists, update the provided fields.

    Status is required on first insert (vocabulary validated in the
    request model). On UPDATE, status remains optional — a PATCH that
    omits status leaves the existing status untouched.
    """
    # Fields the caller explicitly set (omitted ≠ null).
    set_fields = body.model_fields_set

    # Resolve plan_id from the parent task so the caller doesn't have to
    # pass it (and so we keep a single source of truth).
    task_row = await session.get(Task, task_id)
    if task_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id} not found",
        )

    existing = await session.get(TaskBoard, task_id)

    if existing is None:
        if "status" not in set_fields or body.status is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="status is required on first insert",
            )
        row = TaskBoard(
            task_id=task_id,
            plan_id=task_row.plan_id,
            assignee=body.assignee,
            status=body.status,
            branch=body.branch,
            pr_number=body.pr_number,
            notes=body.notes,
            # Set updated_at explicitly so the response carries the value
            # even when the underlying session hasn't flushed yet (the
            # ``server_default=text("now()")`` only fires on DB write).
            updated_at=datetime.now(tz=None).astimezone(),
            updated_by=body.updated_by,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"task_board insert failed: {exc.orig}",
            ) from exc
        existing = row
    else:
        # Selective update: only fields the caller set.
        if "assignee" in set_fields:
            existing.assignee = body.assignee
        if "status" in set_fields and body.status is not None:
            existing.status = body.status
        if "branch" in set_fields:
            existing.branch = body.branch
        if "pr_number" in set_fields:
            existing.pr_number = body.pr_number
        if "notes" in set_fields:
            existing.notes = body.notes
        if "updated_by" in set_fields:
            existing.updated_by = body.updated_by
        # Refresh updated_at on every PATCH so the timestamp reflects
        # the most recent coordinator write even on a no-op payload.
        existing.updated_at = datetime.now(tz=existing.updated_at.tzinfo)
        await session.flush()

    await session.commit()
    await session.refresh(existing)
    # ADR-0084 Task 2B — drop the coordinator-overlay cache entry for
    # this plan so the next cap-check sees the new status (e.g. a fresh
    # ``blocked_operator`` write becomes visible to the dispatch path
    # immediately, not after the 30s TTL).
    invalidate_overlay_cache(existing.plan_id)
    return TaskBoardRow.model_validate(existing, from_attributes=True)
