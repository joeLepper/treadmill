"""``POST /api/v1/dashboard/corpus/{kind}/export`` — corpus exporters.

Per ADR-0070 substep 3 task 4, materializes labeled gold rows into
JSONL files via ``treadmill_api.corpus_export`` and returns the row
count. The CLI (``treadmill corpus export ...``) is the operator-
facing wrapper; this endpoint is the HTTP seam ADR-0010 requires.

Two endpoints, one per gold kind:

  * ``POST /corpus/architect-gold/export``  body ``{out_path: str}``
  * ``POST /corpus/validator-gold/export``  body ``{out_path: str}``

Both return ``{rows_written: int}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.corpus_export import (
    export_architect_gold,
    export_validator_gold,
)
from treadmill_api.dependencies_db import get_session


class ExportRequest(BaseModel):
    out_path: str = Field(
        ..., description=(
            "Absolute or relative path the exporter writes JSONL to. "
            "Parent directory is created if missing."
        ),
    )


class ExportResponse(BaseModel):
    rows_written: int


router = APIRouter()


def _resolve_path(out_path: str) -> Path:
    """Resolve the operator-supplied path. The exporter creates parent
    directories; the FastAPI layer rejects empty strings explicitly."""
    if not out_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="out_path must be a non-empty string",
        )
    return Path(out_path)


@router.post(
    "/corpus/architect-gold/export",
    response_model=ExportResponse,
    status_code=status.HTTP_200_OK,
)
async def post_architect_gold_export(
    body: ExportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExportResponse:
    """Export labeled architect-gold rows to ``body.out_path`` as JSONL.

    Returns the count of rows written.
    """
    count = await export_architect_gold(session, _resolve_path(body.out_path))
    return ExportResponse(rows_written=count)


@router.post(
    "/corpus/validator-gold/export",
    response_model=ExportResponse,
    status_code=status.HTTP_200_OK,
)
async def post_validator_gold_export(
    body: ExportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExportResponse:
    """Export labeled validator-gold rows to ``body.out_path`` as JSONL.

    Returns the count of rows written.
    """
    count = await export_validator_gold(session, _resolve_path(body.out_path))
    return ExportResponse(rows_written=count)
