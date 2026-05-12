"""Hooks router — slug-keyed reusable hook commands per ADR-0010."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Hook


router = APIRouter(prefix="/api/v1/hooks", tags=["hooks"])


class HookCreateRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    event: str = Field(..., min_length=1, max_length=64)
    matcher: str | None = Field(default=None, max_length=255)
    command: str = Field(..., min_length=1)


class HookResponse(BaseModel):
    id: str
    name: str
    event: str
    matcher: str | None
    command: str
    created_at: datetime
    updated_at: datetime


def _to_response(hook: Hook) -> HookResponse:
    return HookResponse(
        id=hook.id, name=hook.name, event=hook.event, matcher=hook.matcher,
        command=hook.command, created_at=hook.created_at, updated_at=hook.updated_at,
    )


@router.post("", response_model=HookResponse, status_code=status.HTTP_201_CREATED)
async def create_hook(
    body: HookCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HookResponse:
    hook = Hook(id=body.id, name=body.name, event=body.event,
                matcher=body.matcher, command=body.command)
    session.add(hook)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"hook {body.id!r} already exists",
        ) from exc
    await session.refresh(hook)
    return _to_response(hook)


@router.get("", response_model=list[HookResponse])
async def list_hooks(session: Annotated[AsyncSession, Depends(get_session)]) -> list[HookResponse]:
    result = await session.execute(select(Hook).order_by(Hook.id))
    return [_to_response(h) for h in result.scalars()]


@router.get("/{hook_id}", response_model=HookResponse)
async def get_hook(
    hook_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HookResponse:
    hook = await session.get(Hook, hook_id)
    if hook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="hook not found")
    return _to_response(hook)
