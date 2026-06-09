"""``/api/v1/team_configs`` + ``/api/v1/queue_depth`` — coordinator/worker
label registry per repo (Task C of the combined ADR-0085+0086 plan).

Four CRUD endpoints on ``team_configs`` (POST upsert / GET list / GET by
repo / DELETE) plus one query endpoint (``GET /api/v1/queue_depth``).

``queue_depth`` excludes tasks where ``tasks.created_by`` matches a
``coordinator_label`` registered in ``team_configs`` — coordinators
register their own brief-emitted tasks, which the operator-facing depth
shouldn't double-count. The query reads from the ``task_status`` view
(columns: ``id``, ``derived_status`` — NOT ``task_id`` / ``status``;
``tasks`` itself has no status column).

The ``{repo:path}`` path-converter on the per-repo routes lets repos
with slashes (``owner/name``) round-trip cleanly without URL-encoding
gymnastics.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.team_config_store import TeamConfigStore


router = APIRouter(prefix="/api/v1", tags=["team_configs"])

_store = TeamConfigStore()


class TeamConfigRow(BaseModel):
    """Wire representation of one ``team_configs`` row."""

    id: uuid.UUID
    repo: str
    coordinator_label: str
    worker_labels: list[str]
    created_at: datetime
    updated_at: datetime


class TeamConfigUpsert(BaseModel):
    repo: str = Field(min_length=1, max_length=255)
    coordinator_label: str = Field(min_length=1, max_length=64)
    worker_labels: list[str] = Field(default_factory=list)


class QueueDepth(BaseModel):
    visible: int
    in_flight: int


@router.post(
    "/team_configs",
    response_model=TeamConfigRow,
    status_code=status.HTTP_200_OK,
)
async def upsert_team_config(
    body: TeamConfigUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TeamConfigRow:
    row = await _store.upsert(
        session,
        repo=body.repo,
        coordinator_label=body.coordinator_label,
        worker_labels=body.worker_labels,
    )
    await session.commit()
    return TeamConfigRow.model_validate(row, from_attributes=True)


@router.get("/team_configs", response_model=list[TeamConfigRow])
async def list_team_configs(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TeamConfigRow]:
    rows = await _store.list_all(session)
    return [TeamConfigRow.model_validate(r, from_attributes=True) for r in rows]


@router.get("/team_configs/{repo:path}", response_model=TeamConfigRow)
async def get_team_config(
    repo: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TeamConfigRow:
    row = await _store.get_by_repo(session, repo)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_config for repo {repo!r} not found",
        )
    return TeamConfigRow.model_validate(row, from_attributes=True)


@router.delete(
    "/team_configs/{repo:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_team_config(
    repo: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    deleted = await _store.delete(session, repo)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_config for repo {repo!r} not found",
        )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_QUEUE_DEPTH_SQL = text(
    """
    SELECT
        COUNT(*) FILTER (WHERE ts.derived_status = 'registered')      AS visible,
        COUNT(*) FILTER (WHERE ts.derived_status LIKE '%: executing') AS in_flight
    FROM task_status ts
    LEFT JOIN tasks t ON t.id = ts.id
    WHERE COALESCE(t.created_by, '') NOT IN (
        SELECT coordinator_label FROM team_configs
    )
    """
)


@router.get("/queue_depth", response_model=QueueDepth)
async def get_queue_depth(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> QueueDepth:
    """Visible + in-flight task counts, excluding coordinator-authored tasks.

    Coordinators emit their own brief tasks via ``created_by =
    <coordinator_label>``. Those tasks already have a routing owner; the
    operator-facing depth shows only tasks that need triage attention.
    """
    result = await session.execute(_QUEUE_DEPTH_SQL)
    row = result.one()
    return QueueDepth(visible=int(row.visible or 0), in_flight=int(row.in_flight or 0))
