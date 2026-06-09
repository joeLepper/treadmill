"""Coordinator workflow-run lifecycle router (ADR-0085+0086 plan, Task B).

Two endpoints the coordinator uses to register and update Treadmill
lifecycle state on behalf of its orchestrators:

* ``POST /api/v1/workflow_runs`` — creates one ``workflow_runs`` row +
  one ``workflow_run_steps`` row (``step_name="author"``,
  ``status="pending"``) for a given task. Returns the new IDs so the
  coordinator can later PATCH the step row as the orchestrator's work
  progresses. 409 if a run already exists for the task; 404 if the
  task is unknown.

* ``PATCH /api/v1/workflow_run_steps/{step_id}`` — partial update of a
  step's lifecycle columns (``status``, ``started_at``,
  ``completed_at``). 404 if the step is unknown.

Per ADR-0085+0086 the coordinator owns all Treadmill bookkeeping for
the repos it manages — orchestrators only run code + open PRs. This
router is the only HTTP write surface for the workflow_runs +
workflow_run_steps tables; the event-driven projection path (per
ADR-0011) remains the writer for SQS-derived state. The two writers
do not overlap: the coordinator writes ``status="pending"`` /
``"running"`` on the up-front step row, the projection writes
``"completed"`` / ``"failed"`` when the orchestrator's ``step.*``
events land on the bus.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Task, WorkflowRun, WorkflowRunStep


router = APIRouter(prefix="/api/v1", tags=["workflow_runs"])


# ── Request / response models ─────────────────────────────────────────────


class CreateWorkflowRunRequest(BaseModel):
    """POST body for ``/api/v1/workflow_runs``."""

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    trigger: str = "coordinator"


class CreateWorkflowRunResponse(BaseModel):
    """Identities returned so the coordinator can patch the step later."""

    run_id: uuid.UUID
    step_id: uuid.UUID


class UpdateWorkflowRunStepRequest(BaseModel):
    """PATCH body for ``/api/v1/workflow_run_steps/{step_id}``.

    All three fields are optional; only the keys present in the body
    are written. Unknown keys 422.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "running", "completed", "failed", "cancelled"] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


# ── Routes ────────────────────────────────────────────────────────────────


# ADR-0086 commitment: the coordinator's up-front step always carries
# ``step_name="author"``; the canonical role for that step is
# ``role-code-author`` (see starters.py wf-code-author definition).
# The coordinator does not currently dispatch any other workflow type
# through this surface; if it needs to in the future, derive the
# role_id from the task's workflow_version definition rather than
# extending this constant.
_COORDINATOR_AUTHOR_STEP_NAME = "author"
_COORDINATOR_AUTHOR_ROLE_ID = "role-code-author"


@router.post(
    "/workflow_runs",
    response_model=CreateWorkflowRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow_run(
    body: CreateWorkflowRunRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CreateWorkflowRunResponse:
    """Register a new workflow_runs row + author step on behalf of the coordinator.

    The new ``WorkflowRun`` inherits the task's
    ``workflow_version_id`` so the worker-context fetch
    (``GET /api/v1/steps/{step_id}``) resolves the same workflow the
    plan was submitted against.
    """
    task = await session.get(Task, body.task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {body.task_id!s} not found",
        )

    # 409 if a run already exists — coordinator must not double-register.
    existing = await session.execute(
        select(WorkflowRun.id).where(WorkflowRun.task_id == body.task_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"workflow_run already exists for task {body.task_id!s}; "
                "coordinator must not register a second run for the same task"
            ),
        )

    run = WorkflowRun(
        task_id=body.task_id,
        workflow_version_id=task.workflow_version_id,
        trigger=body.trigger,
    )
    session.add(run)
    await session.flush()  # populate run.id (server-default UUID).

    step = WorkflowRunStep(
        run_id=run.id,
        step_index=0,
        step_name=_COORDINATOR_AUTHOR_STEP_NAME,
        role_id=_COORDINATOR_AUTHOR_ROLE_ID,
        status="pending",
    )
    session.add(step)
    await session.flush()  # populate step.id.
    await session.commit()

    return CreateWorkflowRunResponse(run_id=run.id, step_id=step.id)


@router.patch("/workflow_run_steps/{step_id}", status_code=status.HTTP_200_OK)
async def update_workflow_run_step(
    step_id: uuid.UUID,
    body: UpdateWorkflowRunStepRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Partial-update a step's lifecycle columns.

    Only the keys present in the request body are written.
    ``model_fields_set`` distinguishes ``"started_at": null`` (clear)
    from ``"started_at"`` omitted (leave untouched).
    """
    step = await session.get(WorkflowRunStep, step_id)
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"workflow_run_step {step_id!s} not found",
        )

    fields_set = body.model_fields_set
    if "status" in fields_set and body.status is not None:
        step.status = body.status
    if "started_at" in fields_set:
        step.started_at = body.started_at
    if "completed_at" in fields_set:
        step.completed_at = body.completed_at

    await session.commit()
    return {
        "step_id": str(step.id),
        "status": step.status,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "completed_at": step.completed_at.isoformat() if step.completed_at else None,
    }
