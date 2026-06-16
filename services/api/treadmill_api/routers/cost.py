"""``/api/v1/cost/rollup`` — cost decomposition for the economics screen.

Returns the aggregates the per-label ``/llm_calls/report`` doesn't: a daily
token series split by model, a by-model rollup, and the count of merged
outcomes (``pr_merged`` events) in the window. Returns raw token sums; the
client prices them via its pricing table (pricing stays in one place).
``cost per merged outcome`` = window spend / outcomes_merged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Event, LLMCall

router = APIRouter(prefix="/api/v1", tags=["cost"])


class DailyTokens(BaseModel):
    day: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


class ModelTokens(BaseModel):
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


class CostRollup(BaseModel):
    since: datetime
    daily: list[DailyTokens]
    by_model: list[ModelTokens]
    outcomes_merged: int


@router.get("/cost/rollup", response_model=CostRollup)
async def cost_rollup(
    session: Annotated[AsyncSession, Depends(get_session)],
    since: Annotated[datetime, Query()],
) -> CostRollup:
    ts = func.coalesce(LLMCall.called_at, LLMCall.created_at)

    daily_rows = (
        await session.execute(
            select(
                func.date_trunc("day", ts).label("d"),
                LLMCall.model,
                func.count().label("calls"),
                func.sum(LLMCall.input_tokens),
                func.sum(LLMCall.output_tokens),
                func.sum(func.coalesce(LLMCall.cache_read_tokens, 0)),
            )
            .where(ts >= since)
            .group_by("d", LLMCall.model)
            .order_by("d")
        )
    ).all()
    daily = [
        DailyTokens(day=d.isoformat(), model=model, calls=calls, input_tokens=int(inp or 0), output_tokens=int(out or 0), cache_read_tokens=int(cr or 0))
        for d, model, calls, inp, out, cr in daily_rows
    ]

    model_rows = (
        await session.execute(
            select(
                LLMCall.model,
                func.count().label("calls"),
                func.sum(LLMCall.input_tokens),
                func.sum(LLMCall.output_tokens),
                func.sum(func.coalesce(LLMCall.cache_read_tokens, 0)),
            )
            .where(ts >= since)
            .group_by(LLMCall.model)
            .order_by(func.sum(LLMCall.input_tokens).desc())
        )
    ).all()
    by_model = [
        ModelTokens(model=model, calls=calls, input_tokens=int(inp or 0), output_tokens=int(out or 0), cache_read_tokens=int(cr or 0))
        for model, calls, inp, out, cr in model_rows
    ]

    outcomes = await session.scalar(
        select(func.count()).select_from(Event).where(Event.action == "pr_merged", Event.created_at >= since)
    )

    return CostRollup(since=since, daily=daily, by_model=by_model, outcomes_merged=int(outcomes or 0))
