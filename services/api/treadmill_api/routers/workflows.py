"""Workflows + WorkflowVersions router per ADR-0010.

A Workflow is a slug-keyed mutable record. Versions are immutable; the
POST-versions endpoint atomically inserts a new version row + its step
list, with version numbers auto-assigned (current max + 1).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Role, Workflow, WorkflowVersion, WorkflowVersionStep


router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


class WorkflowCreateRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    description: str | None = None


class WorkflowResponse(BaseModel):
    id: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    latest_version: int | None = None


class WorkflowStepRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    role_id: str = Field(..., min_length=1, max_length=64)


class WorkflowVersionCreateRequest(BaseModel):
    steps: list[WorkflowStepRequest] = Field(..., min_length=1)


class WorkflowVersionStepResponse(BaseModel):
    step_index: int
    step_name: str
    role_id: str


class WorkflowVersionResponse(BaseModel):
    id: uuid.UUID
    workflow_id: str
    version: int
    created_at: datetime
    steps: list[WorkflowVersionStepResponse]


async def _latest_version_number(session: AsyncSession, workflow_id: str) -> int | None:
    result = await session.execute(
        select(func.max(WorkflowVersion.version))
        .where(WorkflowVersion.workflow_id == workflow_id)
    )
    return result.scalar()


def _to_workflow_response(wf: Workflow, latest: int | None) -> WorkflowResponse:
    return WorkflowResponse(
        id=wf.id, description=wf.description,
        created_at=wf.created_at, updated_at=wf.updated_at,
        latest_version=latest,
    )


async def _load_version_steps(
    session: AsyncSession, version_id: uuid.UUID
) -> list[WorkflowVersionStepResponse]:
    result = await session.execute(
        select(WorkflowVersionStep)
        .where(WorkflowVersionStep.workflow_version_id == version_id)
        .order_by(WorkflowVersionStep.step_index)
    )
    return [
        WorkflowVersionStepResponse(
            step_index=s.step_index, step_name=s.step_name, role_id=s.role_id,
        )
        for s in result.scalars()
    ]


# ── Workflow endpoints ───────────────────────────────────────────────────────


@router.post("", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    body: WorkflowCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkflowResponse:
    workflow = Workflow(id=body.id, description=body.description)
    session.add(workflow)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"workflow {body.id!r} already exists",
        ) from exc
    await session.refresh(workflow)
    return _to_workflow_response(workflow, latest=None)


@router.get("", response_model=list[WorkflowResponse])
async def list_workflows(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[WorkflowResponse]:
    result = await session.execute(select(Workflow).order_by(Workflow.id))
    out: list[WorkflowResponse] = []
    for wf in result.scalars():
        latest = await _latest_version_number(session, wf.id)
        out.append(_to_workflow_response(wf, latest))
    return out


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkflowResponse:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    latest = await _latest_version_number(session, workflow.id)
    return _to_workflow_response(workflow, latest)


# ── Version endpoints ────────────────────────────────────────────────────────


@router.post(
    "/{workflow_id}/versions",
    response_model=WorkflowVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow_version(
    workflow_id: str,
    body: WorkflowVersionCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkflowVersionResponse:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")

    # Validate every referenced role exists.
    role_ids = [step.role_id for step in body.steps]
    if role_ids:
        found = {
            r for r in (
                await session.execute(select(Role.id).where(Role.id.in_(role_ids)))
            ).scalars()
        }
        missing = set(role_ids) - found
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown role(s): {sorted(missing)}",
            )

    next_version = (await _latest_version_number(session, workflow_id) or 0) + 1
    version = WorkflowVersion(workflow_id=workflow_id, version=next_version)
    session.add(version)
    await session.flush()

    for idx, step in enumerate(body.steps):
        session.add(
            WorkflowVersionStep(
                workflow_version_id=version.id,
                step_index=idx,
                step_name=step.name,
                role_id=step.role_id,
            )
        )
    await session.commit()
    await session.refresh(version)
    steps = await _load_version_steps(session, version.id)
    return WorkflowVersionResponse(
        id=version.id, workflow_id=version.workflow_id,
        version=version.version, created_at=version.created_at, steps=steps,
    )


@router.get(
    "/{workflow_id}/versions",
    response_model=list[WorkflowVersionResponse],
)
async def list_workflow_versions(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[WorkflowVersionResponse]:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    result = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version)
    )
    out: list[WorkflowVersionResponse] = []
    for ver in result.scalars():
        steps = await _load_version_steps(session, ver.id)
        out.append(
            WorkflowVersionResponse(
                id=ver.id, workflow_id=ver.workflow_id,
                version=ver.version, created_at=ver.created_at, steps=steps,
            )
        )
    return out
