"""Operator workflow-trigger router (ADR-0053 Wave 3, updated ADR-0057).

Exposes ``POST /api/v1/workflows/{workflow_slug}/trigger`` so an operator
can fire any workflow with a payload, independent of any specific PR task.

Per ADR-0057, this endpoint now creates a synthetic ``Task`` row tied to
the system Plan (sentinel UUID ``00000000-0000-0000-0000-000000000001``)
and dispatches via the existing task-bound ``dispatch_task`` path — workers
never see ``task_id = None``.  The response includes both the new task's id
and the resulting run's id so callers can inspect, retry, or cancel via
the standard operator CLI affordances.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, DispatchError, get_dispatcher
from treadmill_api.models import Plan, Task, WorkflowVersion
from treadmill_api.seed.system_plan import SYSTEM_PLAN_ID


router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


class WorkflowTriggerRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowTriggerResponse(BaseModel):
    task_id: uuid.UUID
    run_id: uuid.UUID
    workflow_id: str


@router.post(
    "/{workflow_slug}/trigger",
    response_model=WorkflowTriggerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_workflow(
    workflow_slug: str,
    body: WorkflowTriggerRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> WorkflowTriggerResponse:
    """Operator-driven dispatch of any workflow with a free-form payload.

    Looks up the latest ``WorkflowVersion`` for ``workflow_slug`` (404 if
    none), enforces ``payload.repo`` is non-empty (400 otherwise — the
    schedule-payload-needs-repo finding applies equally here), creates a
    synthetic ``Task`` tied to the system Plan (ADR-0057), and dispatches
    via ``dispatch_task``.

    Returns ``{task_id, run_id, workflow_id}`` 201 — callers can use
    ``task_id`` to inspect, retry, or cancel the resulting work via the
    standard operator CLI.
    """
    # ── 1. Resolve WorkflowVersion ────────────────────────────────────────
    wv_result = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == workflow_slug)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    wv = wv_result.scalar_one_or_none()
    if wv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"workflow {workflow_slug!r} not found",
        )

    # ── 2. Require repo in payload ────────────────────────────────────────
    repo = body.payload.get("repo")
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="payload missing required field: repo",
        )

    # ── 3. Verify system Plan is seeded ───────────────────────────────────
    system_plan = await session.get(Plan, SYSTEM_PLAN_ID)
    if system_plan is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "system scheduler Plan not seeded — "
                "restart the API to auto-seed via seed_starters_if_empty"
            ),
        )

    # ── 4. Create synthetic Task + dispatch ───────────────────────────────
    task = Task(
        id=uuid.uuid4(),
        plan_id=SYSTEM_PLAN_ID,
        repo=repo,
        title=f"operator-trigger: {workflow_slug}",
        workflow_version_id=wv.id,
        created_by="operator-trigger",
    )
    session.add(task)
    await session.flush()

    try:
        run_id = await dispatcher.dispatch_task(session, task)
    except DispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc

    await session.commit()

    return WorkflowTriggerResponse(
        task_id=task.id,
        run_id=run_id,
        workflow_id=workflow_slug,
    )
