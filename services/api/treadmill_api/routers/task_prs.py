"""Coordinator PR-registration router (ADR-0085+0086 plan, Task B amendment).

Single endpoint the coordinator calls when an orchestrator reports a PR
open (per ADR-0086 §2):

* ``POST /api/v1/task_prs`` — registers a ``(repo, pr_number) → task_id``
  bridge so downstream webhook handlers can resolve a PR's task
  context (per ADR-0007 + the ``task_prs`` table contract).

  - 201 + the row on success.
  - 409 if a row with the same ``(repo, pr_number)`` composite key
    already exists.
  - 404 if the ``task_id`` is unknown in ``tasks``.

Why this is a coordinator-write surface: today the projection consumer
writes ``task_prs`` when it sees a ``step.completed`` event whose
payload carries a PR number (per ADR-0011's single-writer pattern).
Under ADR-0086, the coordinator's brief-acknowledgement handler is the
canonical surface for "an orchestrator reported a PR opened" — the
coordinator is the place where the PR number first appears in the
system, before any ``step.completed`` event. This router is the HTTP
path coordinators use; the projection path still handles the
worker/SQS lane and is unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Task, TaskPR


router = APIRouter(prefix="/api/v1", tags=["task_prs"])


class CreateTaskPRRequest(BaseModel):
    """POST body for ``/api/v1/task_prs``."""

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(..., min_length=1)
    pr_number: int = Field(..., ge=1)
    task_id: uuid.UUID
    branch: str | None = None


class TaskPRResponse(BaseModel):
    """Row returned on success."""

    repo: str
    pr_number: int
    task_id: uuid.UUID
    branch: str | None
    head_sha: str | None = None
    created_at: datetime
    closed_at: datetime | None


class TaskPRListResponse(BaseModel):
    """The open (or all) task_prs rows for a repo — the PR-state poller's poll set
    (ADR-0113): the poller reads this, then checks each PR's merge + CI on GitHub."""

    task_prs: list[TaskPRResponse]


@router.post(
    "/task_prs",
    response_model=TaskPRResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_task_pr(
    body: CreateTaskPRRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskPRResponse:
    """Register a (repo, pr_number) → task_id bridge."""
    # Task FK precheck — 404 surfaces the misconfig at the coordinator
    # before we hit a Postgres FK violation buried in a transaction.
    task = await session.get(Task, body.task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {body.task_id!s} not found",
        )

    # 409 on composite-key conflict. Two coordinators (or a coordinator
    # + the projection consumer) racing on the same PR number must not
    # silently overwrite each other.
    existing = await session.execute(
        select(TaskPR).where(
            TaskPR.repo == body.repo,
            TaskPR.pr_number == body.pr_number,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"task_prs row already exists for "
                f"({body.repo!r}, #{body.pr_number}); coordinator must not "
                "double-register the same PR"
            ),
        )

    pr_row = TaskPR(
        repo=body.repo,
        pr_number=body.pr_number,
        task_id=body.task_id,
        branch=body.branch,
    )
    session.add(pr_row)
    await session.flush()  # populate created_at from the server default.
    await session.commit()
    await session.refresh(pr_row)

    return TaskPRResponse(
        repo=pr_row.repo,
        pr_number=pr_row.pr_number,
        task_id=pr_row.task_id,
        branch=pr_row.branch,
        head_sha=pr_row.head_sha,
        created_at=pr_row.created_at,
        closed_at=pr_row.closed_at,
    )


@router.get("/task_prs", response_model=TaskPRListResponse)
async def list_task_prs(
    session: Annotated[AsyncSession, Depends(get_session)],
    repo: Annotated[str, Query(min_length=1)],
    open_only: Annotated[bool, Query(alias="open")] = True,
) -> TaskPRListResponse:
    """List a repo's task_prs — the PR-state poller's poll set (ADR-0113).

    Defaults to OPEN rows (``closed_at IS NULL``) to bound the poll set; pass
    ``open=false`` to include closed rows. Repo match is case-insensitive to mirror
    the webhook seam's ``(repo, pr_number)`` resolution. Ordered by pr_number desc."""
    stmt = select(TaskPR).where(func.lower(TaskPR.repo) == repo.lower())
    if open_only:
        stmt = stmt.where(TaskPR.closed_at.is_(None))
    stmt = stmt.order_by(TaskPR.pr_number.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return TaskPRListResponse(
        task_prs=[
            TaskPRResponse(
                repo=r.repo,
                pr_number=r.pr_number,
                task_id=r.task_id,
                branch=r.branch,
                head_sha=r.head_sha,
                created_at=r.created_at,
                closed_at=r.closed_at,
            )
            for r in rows
        ]
    )
