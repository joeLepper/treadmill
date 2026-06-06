"""System status router — autoscaler heartbeat + detector reads.

POST /api/v1/system_status/heartbeat — Write autoscaler state (upsert).
GET  /api/v1/system_status/{family}  — Read current state for detectors.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models.system_status import SystemStatus

router = APIRouter(prefix="/api/v1/system_status", tags=["system_status"])


class HeartbeatRequest(BaseModel):
    """Autoscaler heartbeat write (upsert)."""

    family: str = Field(..., min_length=1, max_length=64)
    worker_count: int = Field(..., ge=0)
    last_spawn_at: datetime | None = None
    last_spawn_error: str | None = None
    consecutive_spawn_failures: int = Field(..., ge=0)


class SystemStatusResponse(BaseModel):
    """Current system status row."""

    family: str
    worker_count: int
    last_spawn_at: datetime | None
    last_spawn_error: str | None
    last_consume_at: datetime | None
    consecutive_spawn_failures: int
    updated_at: datetime

    class Config:
        from_attributes = True


@router.post("/heartbeat", status_code=status.HTTP_200_OK)
async def write_heartbeat(
    body: HeartbeatRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Upsert autoscaler heartbeat row.

    Success path: worker_count + last_spawn_at + consecutive_spawn_failures=0 + error=NULL.
    Failure path: consecutive_spawn_failures += 1 + last_spawn_error=truncated message.
    """
    stmt = select(SystemStatus).where(SystemStatus.family == body.family)
    existing = await session.scalar(stmt)

    if existing:
        existing.worker_count = body.worker_count
        existing.last_spawn_at = body.last_spawn_at
        existing.last_spawn_error = body.last_spawn_error
        existing.consecutive_spawn_failures = body.consecutive_spawn_failures
    else:
        new_row = SystemStatus(
            family=body.family,
            worker_count=body.worker_count,
            last_spawn_at=body.last_spawn_at,
            last_spawn_error=body.last_spawn_error,
            consecutive_spawn_failures=body.consecutive_spawn_failures,
        )
        session.add(new_row)

    await session.commit()
    return {"status": "ok"}


@router.get("/{family}", response_model=SystemStatusResponse)
async def read_system_status(
    family: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SystemStatusResponse:
    """Read current system status for a family.

    Returns 404 if family not found.
    """
    stmt = select(SystemStatus).where(SystemStatus.family == family)
    row = await session.scalar(stmt)

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System status not found for family {family}",
        )

    return SystemStatusResponse.model_validate(row)
