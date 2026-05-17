"""Schedules router — ADR-0035.

CRUD surface for the ``schedules`` table. The scheduler subprocess reads
active rows; this router lets operators manage the schedule catalogue.

GET    /api/v1/schedules         — list active + paused with next_fire_at
POST   /api/v1/schedules         — create
PATCH  /api/v1/schedules/{id}    — pause or resume
DELETE /api/v1/schedules/{id}    — delete
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models.schedule import Schedule

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])


# ── Next-fire helper ─────────────────────────────────────────────────────────


def _expand_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field into a set of matching integer values."""
    result: set[int] = set()
    for part in field.split(","):
        if part == "*":
            result.update(range(lo, hi + 1))
        elif m := re.fullmatch(r"\*/(\d+)", part):
            result.update(range(lo, hi + 1, int(m.group(1))))
        elif m := re.fullmatch(r"(\d+)-(\d+)/(\d+)", part):
            result.update(range(int(m.group(1)), int(m.group(2)) + 1, int(m.group(3))))
        elif m := re.fullmatch(r"(\d+)-(\d+)", part):
            result.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif re.fullmatch(r"\d+", part):
            result.add(int(part))
    return {v for v in result if lo <= v <= hi}


def _next_fire(expr: str, after: datetime) -> datetime | None:
    """Return the next datetime after ``after`` matching a 5-field cron.

    Iterates minute-by-minute; capped at 366 days so untriggerable
    expressions (e.g. ``0 0 31 2 *``) return None rather than looping.

    Standard OR semantics for DOM/DOW: when both are restricted (non-``*``),
    a day matches if EITHER condition is true (Vixie cron behaviour).
    """
    try:
        parts = expr.split()
        if len(parts) != 5:
            return None
        min_f, hr_f, dom_f, mon_f, dow_f = parts
        mins = _expand_field(min_f, 0, 59)
        hrs = _expand_field(hr_f, 0, 23)
        doms = _expand_field(dom_f, 1, 31)
        mons = _expand_field(mon_f, 1, 12)
        # Cron DOW: 0 and 7 are both Sunday; Python weekday: Monday=0..Sunday=6
        dows_cron = _expand_field(dow_f, 0, 7)
        dows_py = {(d - 1) % 7 for d in dows_cron}
        dom_restricted = dom_f != "*"
        dow_restricted = dow_f != "*"

        current = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        deadline = current + timedelta(days=366)

        while current <= deadline:
            if current.month in mons and current.hour in hrs and current.minute in mins:
                if dom_restricted and dow_restricted:
                    day_ok = current.day in doms or current.weekday() in dows_py
                elif dom_restricted:
                    day_ok = current.day in doms
                elif dow_restricted:
                    day_ok = current.weekday() in dows_py
                else:
                    day_ok = True
                if day_ok:
                    return current
            current += timedelta(minutes=1)
        return None
    except (ValueError, AttributeError):
        return None


# ── Pydantic models ──────────────────────────────────────────────────────────


class ScheduleCreateRequest(BaseModel):
    cron_expression: str = Field(..., min_length=1, max_length=128)
    workflow_id: str = Field(..., min_length=1, max_length=64)
    jitter_seconds: int = Field(60, ge=0)
    quiet_hours: str | None = Field(None, max_length=16)
    quiet_tz: str = Field("America/Los_Angeles", max_length=64)
    payload_template: dict[str, Any] = Field(default_factory=dict)
    created_by: str = Field(..., min_length=1, max_length=255)


class SchedulePatchRequest(BaseModel):
    status: Literal["active", "paused"]


class ScheduleResponse(BaseModel):
    id: uuid.UUID
    cron_expression: str
    workflow_id: str
    payload_template: dict[str, Any]
    status: str
    jitter_seconds: int
    quiet_hours: str | None
    quiet_tz: str
    last_fired_at: datetime | None
    created_by: str
    created_at: datetime
    next_fire_at: datetime | None = None


def _to_response(s: Schedule) -> ScheduleResponse:
    now = datetime.now(tz=timezone.utc)
    ref = s.last_fired_at if s.last_fired_at is not None else now
    return ScheduleResponse(
        id=s.id,
        cron_expression=s.cron_expression,
        workflow_id=s.workflow_id,
        payload_template=s.payload_template or {},
        status=s.status,
        jitter_seconds=s.jitter_seconds,
        quiet_hours=s.quiet_hours,
        quiet_tz=s.quiet_tz,
        last_fired_at=s.last_fired_at,
        created_by=s.created_by,
        created_at=s.created_at,
        next_fire_at=_next_fire(s.cron_expression, ref),
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ScheduleResponse])
async def list_schedules(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ScheduleResponse]:
    """List all active and paused schedules with computed next-fire time."""
    result = await session.execute(
        select(Schedule)
        .where(Schedule.status.in_(["active", "paused"]))
        .order_by(Schedule.created_at.desc())
    )
    return [_to_response(s) for s in result.scalars().all()]


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    body: ScheduleCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScheduleResponse:
    """Create a new active schedule."""
    schedule = Schedule(
        cron_expression=body.cron_expression,
        workflow_id=body.workflow_id,
        jitter_seconds=body.jitter_seconds,
        quiet_hours=body.quiet_hours,
        quiet_tz=body.quiet_tz,
        payload_template=body.payload_template,
        created_by=body.created_by,
        status="active",
    )
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return _to_response(schedule)


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def patch_schedule(
    schedule_id: uuid.UUID,
    body: SchedulePatchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScheduleResponse:
    """Pause or resume a schedule."""
    schedule = await session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="schedule not found",
        )
    schedule.status = body.status
    await session.commit()
    await session.refresh(schedule)
    return _to_response(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Permanently delete a schedule."""
    schedule = await session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="schedule not found",
        )
    await session.delete(schedule)
    await session.commit()
