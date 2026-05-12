"""Roles router — slug-keyed agent configuration per ADR-0010.

A Role is ``model + system_prompt + skills + hooks``. Skills and Hooks
are referenced by slug in ordered lists; the router validates that
referenced slugs exist before creating the role. The DB ``compute_tier``
column is reserved for the future multi-tier ADR and is not exposed on
the wire at v0 (decision #12 in 2026-05-11 closure plan).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Hook, Role, RoleHook, RoleSkill, Skill


router = APIRouter(prefix="/api/v1/roles", tags=["roles"])


class RoleCreateRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    model: str = Field(..., min_length=1, max_length=128)
    system_prompt: str
    skills: list[str] = Field(default_factory=list)
    hooks: list[str] = Field(default_factory=list)


class RoleResponse(BaseModel):
    id: str
    model: str
    system_prompt: str
    skills: list[str]
    hooks: list[str]
    created_at: datetime
    updated_at: datetime


async def _load_role_with_refs(session: AsyncSession, role_id: str) -> tuple[Role, list[str], list[str]] | None:
    """Fetch a Role plus its ordered skills + hooks slug lists."""
    role = await session.get(Role, role_id)
    if role is None:
        return None
    skills_q = await session.execute(
        select(RoleSkill.skill_id)
        .where(RoleSkill.role_id == role_id)
        .order_by(RoleSkill.position)
    )
    hooks_q = await session.execute(
        select(RoleHook.hook_id)
        .where(RoleHook.role_id == role_id)
        .order_by(RoleHook.position)
    )
    return role, list(skills_q.scalars()), list(hooks_q.scalars())


def _to_response(role: Role, skills: list[str], hooks: list[str]) -> RoleResponse:
    return RoleResponse(
        id=role.id, model=role.model, system_prompt=role.system_prompt,
        skills=skills, hooks=hooks,
        created_at=role.created_at, updated_at=role.updated_at,
    )


async def _validate_refs_exist(
    session: AsyncSession, skills: list[str], hooks: list[str]
) -> None:
    """Reject role creation if any referenced skill/hook slug does not exist."""
    if skills:
        found = {
            s for s in (
                await session.execute(select(Skill.id).where(Skill.id.in_(skills)))
            ).scalars()
        }
        missing = set(skills) - found
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown skill(s): {sorted(missing)}",
            )
    if hooks:
        found = {
            h for h in (
                await session.execute(select(Hook.id).where(Hook.id.in_(hooks)))
            ).scalars()
        }
        missing = set(hooks) - found
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown hook(s): {sorted(missing)}",
            )


@router.post("", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RoleResponse:
    await _validate_refs_exist(session, body.skills, body.hooks)
    role = Role(
        id=body.id, model=body.model, system_prompt=body.system_prompt,
    )
    session.add(role)
    for idx, skill_id in enumerate(body.skills):
        session.add(RoleSkill(role_id=body.id, skill_id=skill_id, position=idx))
    for idx, hook_id in enumerate(body.hooks):
        session.add(RoleHook(role_id=body.id, hook_id=hook_id, position=idx))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"role {body.id!r} already exists",
        ) from exc

    loaded = await _load_role_with_refs(session, body.id)
    if loaded is None:
        # We just committed this role; if it isn't loadable, the DB is in
        # an inconsistent state — surface it rather than crash on attribute
        # access.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"role {body.id!r} not loadable after commit",
        )
    return _to_response(*loaded)


@router.get("", response_model=list[RoleResponse])
async def list_roles(session: Annotated[AsyncSession, Depends(get_session)]) -> list[RoleResponse]:
    result = await session.execute(select(Role).order_by(Role.id))
    out: list[RoleResponse] = []
    for role in result.scalars():
        loaded = await _load_role_with_refs(session, role.id)
        if loaded is None:
            # The role was returned by the listing query above; if it has
            # vanished between queries the DB is racing with a delete —
            # surface as 500 rather than skip.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"role {role.id!r} not loadable during list",
            )
        out.append(_to_response(*loaded))
    return out


@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RoleResponse:
    loaded = await _load_role_with_refs(session, role_id)
    if loaded is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="role not found")
    return _to_response(*loaded)
