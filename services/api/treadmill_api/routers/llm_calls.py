"""``/api/v1/llm_calls`` — per-subprocess token attribution surface.

Single endpoint:

* ``POST /api/v1/llm_calls`` — worker (via coordinator relay) records one
  row per Claude Code subprocess invocation. Returns 201 + row. 404 when
  the referenced ``task_execution_id`` does not exist.

Token counts come from ``--output-format json`` at subprocess exit. The
FK is to ``task_executions``; ON DELETE CASCADE so cleanup is automatic.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import LLMCall, TaskExecution


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
    task_execution_id: uuid.UUID
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int | None
    cache_read_tokens: int | None
    model: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Endpoint ─────────────────────────────────────────────────────────────


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
