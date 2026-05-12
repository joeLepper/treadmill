"""Tasks router. Read-mostly per the Phase 2 plan; the primary creation
path is via Plans (which spawns Tasks from a parsed plan-doc). This router
exposes ``GET /tasks/{id}`` and ``GET /tasks`` for inspection, and a
small ``POST /tasks`` for direct submissions when the parent Plan exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, DispatchError, get_dispatcher
from treadmill_api.events.task import TaskRegistered
from treadmill_api.models import Plan, Task, Workflow, WorkflowVersion


router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class TaskCreateRequest(BaseModel):
    plan_id: uuid.UUID
    title: str = Field(..., min_length=1, max_length=512)
    description: str | None = None
    workflow: str = Field(..., min_length=1, max_length=64)
    """Workflow slug; the latest version is pinned at submission time."""

    created_by: str | None = None


class TaskResponse(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    repo: str
    title: str
    description: str | None
    workflow_version_id: uuid.UUID
    created_by: str | None
    created_at: datetime
    derived_status: str | None = None
    mergeability: str | None = None
    """The ``derived_mergeability`` from the ``task_mergeability`` VIEW
    (ADR-0013). ``None`` when the task has no PR yet — the VIEW joins
    on ``task_prs`` so there is no row to read. See
    ``GET /tasks/{id}/mergeability`` for the full row."""


class MergeabilityResponse(BaseModel):
    """Focused projection of ``task_mergeability`` (ADR-0013).

    Reserved for a future auto-merge orchestrator's polling per the
    ADR's "auto-merge upgrade path". Single-purpose endpoint — does not
    widen the task contract.
    """

    task_id: uuid.UUID
    repo: str | None
    pr_number: int | None
    head_sha: str | None
    review_decision: str | None
    validate_decision: str | None
    ci_conclusion: str | None
    pr_conflicting: bool | None
    derived_mergeability: str
    """Never null — defaults to ``'pending'`` when no PR row exists."""


def _row_to_response(row) -> TaskResponse:
    return TaskResponse(
        id=row.id, plan_id=row.plan_id, repo=row.repo,
        title=row.title, description=row.description,
        workflow_version_id=row.workflow_version_id,
        created_by=row.created_by, created_at=row.created_at,
        derived_status=row.derived_status,
        mergeability=row.derived_mergeability,
    )


_TASK_WITH_STATUS_SQL = """
    SELECT t.id, t.plan_id, t.repo, t.title, t.description,
           t.workflow_version_id, t.created_by, t.created_at,
           ts.derived_status,
           tm.derived_mergeability
    FROM tasks t
    LEFT JOIN task_status ts ON ts.id = t.id
    LEFT JOIN task_mergeability tm ON tm.task_id = t.id
"""


async def _resolve_workflow_version(session: AsyncSession, slug: str) -> uuid.UUID:
    workflow = await session.get(Workflow, slug)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workflow {slug!r} not registered",
        )
    result = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == slug)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workflow {slug!r} has no versions yet",
        )
    return version.id


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> TaskResponse:
    plan = await session.get(Plan, body.plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"plan {body.plan_id} not found",
        )
    wv_id = await _resolve_workflow_version(session, body.workflow)
    task = Task(
        plan_id=plan.id, repo=plan.repo,
        title=body.title, description=body.description,
        workflow_version_id=wv_id, created_by=body.created_by,
    )
    session.add(task)
    await session.flush()
    # A.6 — emit TaskRegistered before dispatch so the audit log carries
    # the registration before the run is materialized.
    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="registered",
        payload=TaskRegistered(
            repo=task.repo,
            title=task.title,
            workflow_version_id=wv_id,
            plan_id=plan.id,
        ),
        plan_id=plan.id,
        task_id=task.id,
    )
    try:
        await dispatcher.dispatch_task(session, task)
    except DispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    await session.commit()
    await session.refresh(task)

    # Re-fetch with derived_status from the VIEW.
    row = (
        await session.execute(
            text(_TASK_WITH_STATUS_SQL + " WHERE t.id = :id"),
            {"id": task.id},
        )
    ).one()
    return _row_to_response(row)


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    session: Annotated[AsyncSession, Depends(get_session)],
    repo: Annotated[str | None, Query()] = None,
    plan_id: Annotated[uuid.UUID | None, Query()] = None,
    derived_status: Annotated[str | None, Query()] = None,
) -> list[TaskResponse]:
    """List tasks with optional filters by repo, plan_id, or derived_status."""
    sql = _TASK_WITH_STATUS_SQL + " WHERE 1=1"
    params: dict[str, object] = {}
    if repo is not None:
        sql += " AND t.repo = :repo"
        params["repo"] = repo
    if plan_id is not None:
        sql += " AND t.plan_id = :plan_id"
        params["plan_id"] = plan_id
    if derived_status is not None:
        sql += " AND ts.derived_status = :ds"
        params["ds"] = derived_status
    sql += " ORDER BY t.created_at DESC LIMIT 500"
    result = await session.execute(text(sql), params)
    return [_row_to_response(row) for row in result]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskResponse:
    row = (
        await session.execute(
            text(_TASK_WITH_STATUS_SQL + " WHERE t.id = :id"),
            {"id": task_id},
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return _row_to_response(row)


_MERGEABILITY_SQL = """
    SELECT t.id AS task_id,
           tm.repo, tm.pr_number, tm.head_sha,
           tm.review_decision, tm.validate_decision,
           tm.ci_conclusion, tm.pr_conflicting,
           tm.derived_mergeability
    FROM tasks t
    LEFT JOIN task_mergeability tm ON tm.task_id = t.id
    WHERE t.id = :id
"""


@router.get("/{task_id}/mergeability", response_model=MergeabilityResponse)
async def get_task_mergeability(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MergeabilityResponse:
    """Return the ``task_mergeability`` row for a task per ADR-0013.

    The VIEW joins ``task_prs``, so a task with no PR has no row in
    ``task_mergeability``. We surface that as ``derived_mergeability =
    'pending'`` with every other field NULL — the auto-merge orchestrator
    treats it the same as "head sha unknown".
    """

    row = (
        await session.execute(
            text(_MERGEABILITY_SQL),
            {"id": task_id},
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )
    return MergeabilityResponse(
        task_id=row.task_id,
        repo=row.repo,
        pr_number=row.pr_number,
        head_sha=row.head_sha,
        review_decision=row.review_decision,
        validate_decision=row.validate_decision,
        ci_conclusion=row.ci_conclusion,
        pr_conflicting=row.pr_conflicting,
        derived_mergeability=row.derived_mergeability or "pending",
    )
