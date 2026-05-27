"""Operator workflow-trigger router (ADR-0053 Wave 3).

Exposes ``POST /api/v1/workflows/{workflow_slug}/trigger`` so an operator
can fire any workflow with a payload, independent of any task. Shares
the taskless-dispatch path with the scheduler
(``_create_and_publish_run_without_task``) so the run materialization,
``step.ready`` publish, and SQS claim shape stay identical.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.coordination.triggers import (
    _create_and_publish_run_without_task,
)
from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher
from treadmill_api.models import WorkflowVersion


router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


class WorkflowTriggerRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowTriggerResponse(BaseModel):
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
    none), requires ``payload.repo`` to be present (400 otherwise — the
    scheduled-bot path learned the hard way that the taskless dispatch
    needs ``repo`` to populate ``step.ready``), and shares
    ``_create_and_publish_run_without_task`` with the scheduler so both
    surfaces produce identical runs.
    """
    wv_result = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == workflow_slug)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    if wv_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"workflow {workflow_slug!r} not found",
        )

    repo = body.payload.get("repo")
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="payload missing required field: repo",
        )

    run_id = await _create_and_publish_run_without_task(
        session,
        dispatcher,
        workflow_id=workflow_slug,
        trigger="operator:trigger",
        repo=repo,
    )
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workflow {workflow_slug!r} has no version or no steps",
        )

    await session.commit()

    return WorkflowTriggerResponse(run_id=run_id, workflow_id=workflow_slug)
