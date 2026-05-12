"""EventTriggers router — (event_type, repo) → workflow rules per ADR-0007.

A trigger says: when ``event_type`` fires (from a webhook or internal
event), run ``workflow_id`` against the matched task. ``repo=null`` means
"all repos"; specific repo names take precedence in the consumer's
matching logic (added in a later day).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import EventTrigger, Workflow


router = APIRouter(prefix="/api/v1/event-triggers", tags=["event-triggers"])


class EventTriggerCreateRequest(BaseModel):
    repo: str | None = Field(default=None, max_length=255)
    event_type: str = Field(..., min_length=1, max_length=64)
    workflow_id: str = Field(..., min_length=1, max_length=64)
    version_strategy: str = Field(default="latest", max_length=32)
    enabled: bool = True


class EventTriggerResponse(BaseModel):
    id: uuid.UUID
    repo: str | None
    event_type: str
    workflow_id: str
    version_strategy: str
    enabled: bool
    created_at: datetime


def _to_response(trigger: EventTrigger) -> EventTriggerResponse:
    return EventTriggerResponse(
        id=trigger.id, repo=trigger.repo, event_type=trigger.event_type,
        workflow_id=trigger.workflow_id, version_strategy=trigger.version_strategy,
        enabled=trigger.enabled, created_at=trigger.created_at,
    )


@router.post("", response_model=EventTriggerResponse, status_code=status.HTTP_201_CREATED)
async def create_trigger(
    body: EventTriggerCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventTriggerResponse:
    workflow = await session.get(Workflow, body.workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workflow {body.workflow_id!r} not registered",
        )
    trigger = EventTrigger(
        repo=body.repo, event_type=body.event_type,
        workflow_id=body.workflow_id, version_strategy=body.version_strategy,
        enabled=body.enabled,
    )
    session.add(trigger)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"trigger for (repo={body.repo!r}, event_type={body.event_type!r}) "
                "already exists"
            ),
        ) from exc
    await session.refresh(trigger)
    return _to_response(trigger)


@router.get("", response_model=list[EventTriggerResponse])
async def list_triggers(
    session: Annotated[AsyncSession, Depends(get_session)],
    repo: Annotated[str | None, Query()] = None,
    event_type: Annotated[str | None, Query()] = None,
    enabled: Annotated[bool | None, Query()] = None,
) -> list[EventTriggerResponse]:
    stmt = select(EventTrigger)
    if repo is not None:
        stmt = stmt.where(EventTrigger.repo == repo)
    if event_type is not None:
        stmt = stmt.where(EventTrigger.event_type == event_type)
    if enabled is not None:
        stmt = stmt.where(EventTrigger.enabled == enabled)
    stmt = stmt.order_by(EventTrigger.created_at)
    result = await session.execute(stmt)
    return [_to_response(t) for t in result.scalars()]


@router.get("/{trigger_id}", response_model=EventTriggerResponse)
async def get_trigger(
    trigger_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventTriggerResponse:
    trigger = await session.get(EventTrigger, trigger_id)
    if trigger is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trigger not found")
    return _to_response(trigger)
