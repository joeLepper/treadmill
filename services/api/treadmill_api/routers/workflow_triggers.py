"""Operator workflow-trigger router (ADR-0053 Wave 3, ADR-0057).

Exposes ``POST /api/v1/workflows/{workflow_slug}/trigger`` so an operator
can fire any workflow with a payload, independent of any task.

Per ADR-0057, the underlying dispatch creates a **synthetic Task** tied
to the system Plan (``SYSTEM_PLAN_ID``) and runs the normal
``dispatcher.dispatch_task`` path — same code as scheduled ticks. This
closes the 4th silent-failure hole in the scheduler primitive: workers
no longer receive ``task_id=null`` envelopes from this surface.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.coordination.triggers import _dispatch_via_synthetic_task
from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher


router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


class WorkflowTriggerRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowTriggerResponse(BaseModel):
    run_id: uuid.UUID
    task_id: uuid.UUID
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

    Requires ``payload.repo`` (400 otherwise). Creates a synthetic Task
    under the system Plan and shares ``_dispatch_via_synthetic_task``
    with the scheduler so both surfaces produce identical task-bound
    runs. Returns both ``task_id`` and ``run_id`` so operators can grep
    events / status for the dispatch they just kicked off.
    """
    repo = body.payload.get("repo")
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="payload missing required field: repo",
        )

    # Capture the synthetic Task's id by digging it out of the run's
    # workflow_runs row after dispatch. Simpler: we already have it via
    # the helper if we restructure, but for v1 we re-fetch from the run.
    # The helper returns only the run_id today; we extend it inline here
    # without bloating its signature — fetch the run, read its task_id.
    run_id = await _dispatch_via_synthetic_task(
        session,
        dispatcher,
        workflow_id=workflow_slug,
        repo=repo,
        trigger="operator:trigger",
        created_by="operator-trigger",
        title=f"operator-trigger: {workflow_slug}",
    )
    if run_id is None:
        # Same 404 contract as before — no version / no steps surfaces as
        # a 400 because the operator's intent is impossible to satisfy.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"workflow {workflow_slug!r} has no version or no steps "
                "(check that the workflow slug exists and starters have "
                "been seeded)"
            ),
        )

    # Pull the synthetic task_id out of the run row so the operator can
    # join on it (events table, audit log, CLI follow-up).
    from sqlalchemy import select
    from treadmill_api.models import WorkflowRun

    run = (
        await session.execute(
            select(WorkflowRun).where(WorkflowRun.id == run_id)
        )
    ).scalar_one()
    task_id = run.task_id
    assert task_id is not None, (
        "ADR-0057 invariant: synthetic dispatch must produce a task-bound run"
    )

    await session.commit()

    return WorkflowTriggerResponse(
        run_id=run_id, task_id=task_id, workflow_id=workflow_slug,
    )
