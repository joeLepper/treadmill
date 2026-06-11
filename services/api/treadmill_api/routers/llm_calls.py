"""``/api/v1/llm_calls`` — per-call token attribution surface.

Endpoints:

* ``POST /api/v1/llm_calls`` — worker (via coordinator relay) records one
  row per Claude Code subprocess invocation. Returns 201 + row. 404 when
  the referenced ``task_execution_id`` does not exist.
* ``GET /api/v1/llm_calls/harvest_cursors`` — the ADR-0089 harvester's
  read side: per-transcript byte offsets + cumulative malformed-line
  counts, so re-runs parse only bytes appended since the last run.
* ``POST /api/v1/llm_calls/harvest`` — bulk-insert calls parsed from one
  transcript file and advance its cursor, in a single transaction.
  Duplicate (transcript_path, request_id) pairs are dropped by
  ``ON CONFLICT DO NOTHING`` (idempotency backstop for a response that
  straddled the previous run's byte cursor) and reported in the response.
* ``GET /api/v1/llm_calls/report?since=…`` — per-label rollup (calls,
  output, fresh input, cache creation/read, hit ratio) plus the total
  malformed-line count: ADR-0089 requires unparseable transcript lines
  COUNTED AND REPORTED, never silently skipped.

``task_execution_id`` is nullable post-``20260611_0600`` (see the
migration / ``models/llm_call.py`` docstrings for the decision record);
harvested orchestrator calls carry only ``session_label``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import LLMCall, LLMHarvestCursor, TaskExecution


router = APIRouter(prefix="/api/v1", tags=["llm_calls"])


# ── Pydantic schemas ─────────────────────────────────────────────────────


class LLMCallCreate(BaseModel):
    task_execution_id: uuid.UUID
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    model: str = Field(min_length=1)


class LLMCallRow(BaseModel):
    id: uuid.UUID
    task_execution_id: uuid.UUID | None
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int | None
    cache_read_tokens: int | None
    model: str
    created_at: datetime

    model_config = {"from_attributes": True}


class HarvestedCall(BaseModel):
    request_id: str = Field(min_length=1)
    session_label: str = Field(min_length=1)
    task_execution_id: uuid.UUID | None = None
    called_at: datetime
    model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)


class HarvestBatch(BaseModel):
    transcript_path: str = Field(min_length=1)
    byte_offset: int = Field(ge=0)
    malformed_lines_delta: int = Field(default=0, ge=0)
    calls: list[HarvestedCall] = Field(default_factory=list)


class HarvestResult(BaseModel):
    inserted: int
    duplicates: int
    byte_offset: int


class HarvestCursorRow(BaseModel):
    transcript_path: str
    byte_offset: int
    malformed_lines: int

    model_config = {"from_attributes": True}


class TokenReportRow(BaseModel):
    session_label: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cache_hit_ratio: float


class TokenReport(BaseModel):
    since: datetime
    rows: list[TokenReportRow]
    malformed_lines_total: int


def _hit_ratio(input_tokens: int, cache_creation: int, cache_read: int) -> float:
    denominator = input_tokens + cache_creation + cache_read
    if denominator == 0:
        return 0.0
    return cache_read / denominator


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post(
    "/llm_calls",
    response_model=LLMCallRow,
    status_code=status.HTTP_201_CREATED,
)
async def create_llm_call(
    body: LLMCallCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LLMCallRow:
    execution = await session.get(TaskExecution, body.task_execution_id)
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task_execution {body.task_execution_id!s} not found",
        )
    call = LLMCall(
        task_execution_id=body.task_execution_id,
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        cache_creation_tokens=body.cache_creation_tokens,
        cache_read_tokens=body.cache_read_tokens,
        model=body.model,
    )
    session.add(call)
    await session.flush()
    await session.refresh(call)
    await session.commit()
    return LLMCallRow.model_validate(call, from_attributes=True)


@router.get(
    "/llm_calls/harvest_cursors",
    response_model=list[HarvestCursorRow],
)
async def list_harvest_cursors(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[HarvestCursorRow]:
    result = await session.execute(
        select(LLMHarvestCursor).order_by(LLMHarvestCursor.transcript_path)
    )
    rows = result.scalars().all()
    return [HarvestCursorRow.model_validate(r, from_attributes=True) for r in rows]


@router.post(
    "/llm_calls/harvest",
    response_model=HarvestResult,
    status_code=status.HTTP_201_CREATED,
)
async def harvest_llm_calls(
    body: HarvestBatch,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HarvestResult:
    """Insert one transcript file's newly-parsed calls + advance its cursor.

    Single transaction: either the calls land AND the cursor advances, or
    neither — a crash between the two can't strand un-cursored rows.
    """
    execution_ids = {
        c.task_execution_id for c in body.calls if c.task_execution_id is not None
    }
    if execution_ids:
        found = (
            (
                await session.execute(
                    select(TaskExecution.id).where(TaskExecution.id.in_(execution_ids))
                )
            )
            .scalars()
            .all()
        )
        missing = execution_ids - set(found)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"task_executions not found: {sorted(map(str, missing))}",
            )

    inserted = 0
    if body.calls:
        insert_stmt = (
            pg_insert(LLMCall)
            .values(
                [
                    {
                        "transcript_path": body.transcript_path,
                        "request_id": c.request_id,
                        "session_label": c.session_label,
                        "task_execution_id": c.task_execution_id,
                        "called_at": c.called_at,
                        "model": c.model,
                        "input_tokens": c.input_tokens,
                        "output_tokens": c.output_tokens,
                        "cache_creation_tokens": c.cache_creation_tokens,
                        "cache_read_tokens": c.cache_read_tokens,
                    }
                    for c in body.calls
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["transcript_path", "request_id"],
                index_where=LLMCall.transcript_path.isnot(None)
                & LLMCall.request_id.isnot(None),
            )
            .returning(LLMCall.id)
        )
        inserted = len((await session.execute(insert_stmt)).scalars().all())

    cursor_stmt = pg_insert(LLMHarvestCursor).values(
        transcript_path=body.transcript_path,
        byte_offset=body.byte_offset,
        malformed_lines=body.malformed_lines_delta,
        updated_at=func.now(),
    )
    cursor_stmt = cursor_stmt.on_conflict_do_update(
        index_elements=["transcript_path"],
        set_={
            "byte_offset": cursor_stmt.excluded.byte_offset,
            # Cumulative: ADR-0089 says malformed lines are counted, so a
            # delta from each run accumulates rather than overwrites.
            "malformed_lines": LLMHarvestCursor.malformed_lines
            + cursor_stmt.excluded.malformed_lines,
            "updated_at": func.now(),
        },
    )
    await session.execute(cursor_stmt)
    await session.commit()

    return HarvestResult(
        inserted=inserted,
        duplicates=len(body.calls) - inserted,
        byte_offset=body.byte_offset,
    )


@router.get(
    "/llm_calls/report",
    response_model=TokenReport,
)
async def token_report(
    since: datetime,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenReport:
    result = await session.execute(
        select(
            LLMCall.session_label,
            func.count().label("calls"),
            func.coalesce(func.sum(LLMCall.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(LLMCall.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(LLMCall.cache_creation_tokens), 0).label(
                "cache_creation_tokens"
            ),
            func.coalesce(func.sum(LLMCall.cache_read_tokens), 0).label(
                "cache_read_tokens"
            ),
        )
        .where(LLMCall.session_label.isnot(None), LLMCall.called_at >= since)
        .group_by(LLMCall.session_label)
        .order_by(LLMCall.session_label)
    )
    rows = [
        TokenReportRow(
            session_label=r.session_label,
            calls=r.calls,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_creation_tokens=r.cache_creation_tokens,
            cache_read_tokens=r.cache_read_tokens,
            cache_hit_ratio=_hit_ratio(
                r.input_tokens, r.cache_creation_tokens, r.cache_read_tokens
            ),
        )
        for r in result.all()
    ]
    malformed_total = (
        await session.execute(
            select(func.coalesce(func.sum(LLMHarvestCursor.malformed_lines), 0))
        )
    ).scalar_one()
    return TokenReport(since=since, rows=rows, malformed_lines_total=malformed_total)
