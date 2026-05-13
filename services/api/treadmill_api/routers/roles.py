"""Roles router — slug-keyed agent configuration per ADR-0010.

A Role is ``model + system_prompt + skills + hooks``. Skills and Hooks
are referenced by slug in ordered lists; the router validates that
referenced slugs exist before creating the role. The DB ``compute_tier``
column is reserved for the future multi-tier ADR and is not exposed on
the wire at v0 (decision #12 in 2026-05-11 closure plan).

Per ADR-0028 (resolved 2026-05-13), this router also hosts the
prompt-edit surface:

  * ``PATCH /{id}`` mutates ``roles.system_prompt`` and appends a row
    to ``role_versions`` for the audit trail.
  * ``GET /{id}/versions`` lists the version history.
  * ``GET /{id}/versions/{version}`` returns a specific version.

The DB is the source of truth for role prompts; ``starters.py``
becomes a bootstrap fixture only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import (
    Hook,
    OutputKind,
    Role,
    RoleHook,
    RoleSkill,
    RoleVersion,
    Skill,
)


router = APIRouter(prefix="/api/v1/roles", tags=["roles"])


class RoleCreateRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    model: str = Field(..., min_length=1, max_length=128)
    system_prompt: str
    output_kind: OutputKind
    """Per ADR-0022 — required. The runner reads this off the step
    context to pick the right post-Claude-Code disposition handler."""
    skills: list[str] = Field(default_factory=list)
    hooks: list[str] = Field(default_factory=list)


class RoleResponse(BaseModel):
    id: str
    model: str
    system_prompt: str
    output_kind: OutputKind
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
        output_kind=role.output_kind,
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
        output_kind=body.output_kind,
    )
    session.add(role)
    for idx, skill_id in enumerate(body.skills):
        session.add(RoleSkill(role_id=body.id, skill_id=skill_id, position=idx))
    for idx, hook_id in enumerate(body.hooks):
        session.add(RoleHook(role_id=body.id, hook_id=hook_id, position=idx))
    # ADR-0028: every role's audit trail starts at v1 = its initial
    # prompt, regardless of whether the role was created pre- or
    # post-migration. The alembic 0010 backfill handles pre-migration
    # roles; this write handles post-migration ones.
    session.add(RoleVersion(
        role_id=body.id,
        version=1,
        system_prompt=body.system_prompt,
        notes="initial version (from POST /roles)",
        created_by="api",
    ))
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


# ── Prompt edit + version history (ADR-0028) ──────────────────────────────────


class RolePatchRequest(BaseModel):
    """Operator prompt edit per ADR-0028.

    Only ``system_prompt`` is mutable today (Q28.e: roles only, prompt
    only; model + output_kind + skills + hooks stay code-driven via
    bootstrap). ``notes`` and ``pr_url`` are optional audit-trail
    fields per Q28.d — useful when the edit links back to a PR or
    incident, omitted for low-stakes tweaks.
    """

    system_prompt: str = Field(..., min_length=1)
    notes: str | None = Field(default=None, max_length=4000)
    pr_url: str | None = Field(default=None, max_length=512)


class RolePatchResponse(BaseModel):
    """The updated role plus the new version number for client display."""

    role: RoleResponse
    version: int


class RoleVersionSummary(BaseModel):
    """Audit-trail entry without the prompt content. Used by the
    ``versions`` listing endpoint to keep payload bounded."""

    version: int
    notes: str | None
    pr_url: str | None
    created_at: datetime
    created_by: str | None


class RoleVersionDetail(BaseModel):
    """Full version row including the prompt content. Used by the
    single-version GET endpoint when the operator wants to inspect
    or copy a past prompt."""

    version: int
    system_prompt: str
    notes: str | None
    pr_url: str | None
    created_at: datetime
    created_by: str | None


@router.patch("/{role_id}", response_model=RolePatchResponse)
async def update_role(
    role_id: str,
    body: RolePatchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RolePatchResponse:
    """Mutate ``roles.system_prompt`` and append a new ``role_versions``
    row capturing the change.

    Transaction ordering:

      1. ``SELECT FOR UPDATE`` on the role row to serialize concurrent
         edits (a multi-replica API has more than one writer; today's
         dev-local is single-replica but we lock anyway so the future
         doesn't surprise us).
      2. ``SELECT max(version)`` for this role's version history.
      3. ``INSERT`` the new ``role_versions`` row with version = max+1.
      4. ``UPDATE`` ``roles.system_prompt`` + ``updated_at``.
      5. Commit (via FastAPI's dependency-managed session).

    On a UNIQUE constraint violation against ``(role_id, version)``
    (a concurrent edit raced past the row-lock — should be
    impossible under FOR UPDATE, but defensive), surface 409 so the
    client retries.
    """
    # 1. Row-lock the role to serialize edits.
    role = (
        await session.execute(
            select(Role).where(Role.id == role_id).with_for_update()
        )
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="role not found",
        )

    # 2. Compute the next version. The alembic 0010 backfill ensures
    # at least v1 exists for every role; max() therefore always
    # returns an int (not None) after the backfill lands.
    max_version = (
        await session.execute(
            select(func.coalesce(func.max(RoleVersion.version), 0))
            .where(RoleVersion.role_id == role_id)
        )
    ).scalar_one()
    new_version = int(max_version) + 1

    # 3. Insert the audit-trail row.
    rv = RoleVersion(
        role_id=role_id,
        version=new_version,
        system_prompt=body.system_prompt,
        notes=body.notes,
        pr_url=body.pr_url,
        created_by="api",  # placeholder until auth lands; tracks origin
    )
    session.add(rv)

    # 4. Update the live row + bump updated_at. The schema's
    # server_default=now() only fires on INSERT; updates need an
    # explicit value.
    role.system_prompt = body.system_prompt
    role.updated_at = datetime.now(tz=timezone.utc)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"concurrent update raced; retry the PATCH",
        ) from exc

    loaded = await _load_role_with_refs(session, role_id)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"role {role_id!r} vanished after PATCH",
        )
    return RolePatchResponse(role=_to_response(*loaded), version=new_version)


@router.get("/{role_id}/versions", response_model=list[RoleVersionSummary])
async def list_role_versions(
    role_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[RoleVersionSummary]:
    """List the version history for a role, newest first.

    Returns ``[]`` if the role exists but has no versions (impossible
    after the alembic 0010 backfill, but defensive). Returns 404 if
    the role itself does not exist."""
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="role not found",
        )
    result = await session.execute(
        select(RoleVersion)
        .where(RoleVersion.role_id == role_id)
        .order_by(RoleVersion.version.desc())
    )
    return [
        RoleVersionSummary(
            version=rv.version,
            notes=rv.notes,
            pr_url=rv.pr_url,
            created_at=rv.created_at,
            created_by=rv.created_by,
        )
        for rv in result.scalars()
    ]


@router.get(
    "/{role_id}/versions/{version}", response_model=RoleVersionDetail,
)
async def get_role_version(
    role_id: str,
    version: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RoleVersionDetail:
    """Return a specific version row including its system_prompt.

    Returns 404 if either the role or the (role, version) pair is
    not found."""
    rv = (
        await session.execute(
            select(RoleVersion).where(
                RoleVersion.role_id == role_id,
                RoleVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if rv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"role version not found: role_id={role_id!r} "
                   f"version={version}",
        )
    return RoleVersionDetail(
        version=rv.version,
        system_prompt=rv.system_prompt,
        notes=rv.notes,
        pr_url=rv.pr_url,
        created_at=rv.created_at,
        created_by=rv.created_by,
    )
