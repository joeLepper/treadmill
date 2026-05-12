"""Skills router — slug-keyed reusable skill content per ADR-0010."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Skill


router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


class SkillCreateRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1)


class SkillResponse(BaseModel):
    id: str
    name: str
    content: str
    created_at: datetime
    updated_at: datetime


def _to_response(skill: Skill) -> SkillResponse:
    return SkillResponse(
        id=skill.id, name=skill.name, content=skill.content,
        created_at=skill.created_at, updated_at=skill.updated_at,
    )


@router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: SkillCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SkillResponse:
    skill = Skill(id=body.id, name=body.name, content=body.content)
    session.add(skill)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"skill {body.id!r} already exists",
        ) from exc
    await session.refresh(skill)
    return _to_response(skill)


@router.get("", response_model=list[SkillResponse])
async def list_skills(session: Annotated[AsyncSession, Depends(get_session)]) -> list[SkillResponse]:
    result = await session.execute(select(Skill).order_by(Skill.id))
    return [_to_response(s) for s in result.scalars()]


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(
    skill_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SkillResponse:
    skill = await session.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="skill not found")
    return _to_response(skill)
