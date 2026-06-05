"""``GET /api/v1/review/validator-gold/...`` — ADR-0070 validator-gold labeling.

Provides four endpoints for the validator-gold review queue:

  * ``GET  /next?limit=N``     — N unlabeled rows, lowest-confidence first.
  * ``GET  /stats``            — queue health counters + accuracy fractions.
  * ``GET  /{row_id}``         — single row by UUID; 404 when missing.
  * ``POST /{row_id}/label``   — stamp operator verdict; 404/409 on errors.

The ordering for ``/next`` encodes ``llm_confidence`` as a CASE expression
(``low=0, medium=1, high=2``) so ``ORDER BY ASC`` surfaces least-confident
proposals first — the highest-leverage labeling time per ADR-0070.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models.validator_gold import ValidatorGoldRow

router = APIRouter(prefix="/validator-gold", tags=["review"])

# ── Closed-enum literals ──────────────────────────────────────────────────────

LabelVerdictT = Literal["correct-verdict", "wrong-verdict", "unclear"]

# ── Pydantic models ───────────────────────────────────────────────────────────


class ValidatorGoldRowOut(BaseModel):
    """Response shape for one validator-gold row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    source_run_id: uuid.UUID | None = None
    source_event_id: uuid.UUID | None = None
    source_url: str | None = None
    source_pr_number: int | None = None

    source_step_id: uuid.UUID
    verdict_emitted: str
    script_excerpt: str
    artifact_excerpt: str

    llm_label: str
    llm_confidence: str
    llm_rationale: str
    llm_prompt_version: str
    llm_model: str

    label_verdict: str | None = None
    label_notes: str | None = None
    label_override_reason: str | None = None
    labeled_by: str | None = None
    labeled_at: datetime | None = None
    label_guidelines_version: str | None = None

    outcome_state: str | None = None
    outcome_pr_merged_at: datetime | None = None


class LabelRequest(BaseModel):
    """Operator-supplied verdict for one validator-gold row.

    ``label`` is the only required field — ``labeled_by`` carries
    operator attribution for corpus provenance. ``override_reason`` is
    optional (required by some future kinds where ``label != llm_label``;
    this kind does not enforce that cross-field rule).
    """

    label: LabelVerdictT
    override_reason: str | None = None
    notes: str | None = None
    labeled_by: str = Field(..., min_length=1)


class StatsResponse(BaseModel):
    """Queue health counters and accuracy fractions."""

    total: int
    unlabeled: int
    labeled_total: int
    label_accuracy: float | None
    accuracy_last_100: float | None


# ── Confidence ordering expression ───────────────────────────────────────────

def _confidence_order():  # type: ignore[return]
    """CASE expression mapping low=0, medium=1, high=2 for ASC sort."""
    return case(
        (ValidatorGoldRow.llm_confidence == "low", 0),
        (ValidatorGoldRow.llm_confidence == "medium", 1),
        (ValidatorGoldRow.llm_confidence == "high", 2),
        else_=3,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/next", response_model=list[ValidatorGoldRowOut])
async def get_next(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[ValidatorGoldRowOut]:
    """Return up to ``limit`` unlabeled rows, lowest-confidence first.

    The partial index ``ix_validator_gold_rows_unlabeled`` keeps the
    WHERE clause constant-time. Confidence is encoded as a CASE expression
    so ``ORDER BY ASC`` yields ``low → medium → high``.
    """
    stmt = (
        select(ValidatorGoldRow)
        .where(ValidatorGoldRow.label_verdict.is_(None))
        .order_by(_confidence_order().asc(), ValidatorGoldRow.created_at.asc())
        .limit(limit)
    )
    rows = (await session.scalars(stmt)).all()
    return [ValidatorGoldRowOut.model_validate(r) for r in rows]


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StatsResponse:
    """Return queue health counters and LLM-accuracy fractions.

    ``label_accuracy`` is the fraction of labeled rows where the operator's
    verdict matches the LLM label. ``accuracy_last_100`` is the same
    fraction restricted to the 100 most recently labeled rows by
    ``labeled_at DESC``.
    """
    all_rows = (await session.scalars(select(ValidatorGoldRow))).all()

    total = len(all_rows)
    labeled = [r for r in all_rows if r.label_verdict is not None]
    labeled_total = len(labeled)
    unlabeled = total - labeled_total

    if labeled_total > 0:
        matched = sum(1 for r in labeled if r.label_verdict == r.llm_label)
        label_accuracy: float | None = matched / labeled_total
    else:
        label_accuracy = None

    last_100 = sorted(
        labeled,
        key=lambda r: r.labeled_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:100]

    if last_100:
        matched_100 = sum(1 for r in last_100 if r.label_verdict == r.llm_label)
        accuracy_last_100: float | None = matched_100 / len(last_100)
    else:
        accuracy_last_100 = None

    return StatsResponse(
        total=total,
        unlabeled=unlabeled,
        labeled_total=labeled_total,
        label_accuracy=label_accuracy,
        accuracy_last_100=accuracy_last_100,
    )


@router.get("/{row_id}", response_model=ValidatorGoldRowOut)
async def get_row(
    row_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ValidatorGoldRowOut:
    """Fetch one row by UUID. Returns 404 when not found."""
    row = (
        await session.scalars(
            select(ValidatorGoldRow).where(ValidatorGoldRow.id == row_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"validator-gold row {row_id} not found",
        )
    return ValidatorGoldRowOut.model_validate(row)


@router.post(
    "/{row_id}/label",
    response_model=ValidatorGoldRowOut,
    status_code=status.HTTP_200_OK,
)
async def label_row(
    row_id: uuid.UUID,
    body: LabelRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ValidatorGoldRowOut:
    """Stamp an operator verdict on ``row_id`` and return the updated row.

    Returns 404 when ``row_id`` doesn't exist; 409 when the row already
    carries a non-null ``label_verdict``.
    """
    row = (
        await session.scalars(
            select(ValidatorGoldRow).where(ValidatorGoldRow.id == row_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"validator-gold row {row_id} not found",
        )
    if row.label_verdict is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"validator-gold row {row_id} already labeled",
        )

    row.label_verdict = body.label
    row.label_notes = body.notes
    row.label_override_reason = body.override_reason
    row.labeled_by = body.labeled_by
    row.labeled_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(row)
    return ValidatorGoldRowOut.model_validate(row)
