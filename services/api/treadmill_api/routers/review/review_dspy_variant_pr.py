"""``/api/v1/review/dspy-variant-pr/...`` — operator labeling for DSPy variant PRs.

ADR-0070 substep 4.2.  Backs the operator review UI that walks the queue
of ``review_dspy_variant_pr`` rows emitted by the optimizer.  Auto-
mounted by ``routers/review/__init__.py``'s ``pkgutil`` walk — this
module exposes a module-level ``router = APIRouter(...)`` with the
``/dspy-variant-pr`` sub-prefix and the aggregator appends ``/api/v1/review``.

The factory at ``routers/review/base.py`` (``build_review_router``) is
deliberately bypassed here because this kind has a cross-field validation
rule (``override_reason`` required when the operator's verdict disagrees
with the LLM's recommendation) that the generic factory does not model.
A standalone module is cheaper than widening the factory for one kind.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models.review_dspy_variant_pr import ReviewDspyVariantPrRow
from treadmill_api.schemas.review_dspy_variant_pr import (
    LabelDspyVariantPrRequest,
    ReviewDspyVariantPr,
)
from treadmill_api.services.review_stats import StatsResponse


# ADR-0070 §"Labeled metadata" — bumping this string lets future rubric
# revisions be detected on a row-by-row basis without backfilling.
LABEL_GUIDELINES_VERSION = "v1"


router = APIRouter(prefix="/dspy-variant-pr", tags=["review", "dspy-variant-pr"])


# ── Store seam ────────────────────────────────────────────────────────────────
# The router delegates SQL to ``ReviewDspyVariantPrStore`` so unit tests can
# patch the class at the module seam (mirrors ``triage.labels`` /
# ``TriageStore``) and assert HTTP-shape + call kwargs without a live DB.


class ReviewDspyVariantPrStore:
    """Async accessor for ``review_dspy_variant_pr``.

    All methods take an ``AsyncSession``; the caller owns the transaction.
    """

    async def get_next(
        self, session: AsyncSession, *, limit: int
    ) -> list[ReviewDspyVariantPrRow]:
        """Return up to ``limit`` unlabeled rows, least-confident first.

        Confidence ordering uses a CASE expression mapping
        ``low→0, medium→1, high→2`` so PostgreSQL orders the queue with
        the LLM's least-certain proposals at the top.  Ties break on
        ``created_at`` ASC so older rows surface first within a confidence
        bucket.
        """
        confidence_rank = case(
            (ReviewDspyVariantPrRow.llm_confidence == "low", 0),
            (ReviewDspyVariantPrRow.llm_confidence == "medium", 1),
            (ReviewDspyVariantPrRow.llm_confidence == "high", 2),
            else_=3,
        )
        stmt = (
            select(ReviewDspyVariantPrRow)
            .where(ReviewDspyVariantPrRow.label_verdict.is_(None))
            .order_by(confidence_rank.asc(), ReviewDspyVariantPrRow.created_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(
        self, session: AsyncSession, row_id: uuid.UUID
    ) -> ReviewDspyVariantPrRow | None:
        """Return one row by primary key, or ``None`` if missing."""
        result = await session.execute(
            select(ReviewDspyVariantPrRow).where(
                ReviewDspyVariantPrRow.id == row_id
            )
        )
        return result.scalars().one_or_none()

    async def record_label(
        self,
        session: AsyncSession,
        row_id: uuid.UUID,
        *,
        label_verdict: str,
        label_notes: str | None,
        label_override_reason: str | None,
        labeled_by: str,
    ) -> None:
        """Persist operator label fields + server-stamped ``labeled_at``.

        Uses ``func.now()`` so PostgreSQL writes the timestamp — keeps the
        labeling clock authoritative on the server even when the API
        process clock drifts.  Stamps ``label_guidelines_version`` from
        the module-level constant so a later rubric bump is detectable.
        """
        await session.execute(
            update(ReviewDspyVariantPrRow)
            .where(ReviewDspyVariantPrRow.id == row_id)
            .values(
                label_verdict=label_verdict,
                label_notes=label_notes,
                label_override_reason=label_override_reason,
                labeled_by=labeled_by,
                labeled_at=func.now(),
                label_guidelines_version=LABEL_GUIDELINES_VERSION,
            )
        )

    async def stats(self, session: AsyncSession) -> StatsResponse:
        """Aggregate label statistics for this kind.

        Two SELECTs (one for the overall bucket counts, one over the
        most-recent-100 labeled subquery) — neither pulls rows into Python.
        """
        T = ReviewDspyVariantPrRow

        # ── Overall bucket counts in one SELECT ───────────────────────────────
        overall_row = (
            await session.execute(
                select(
                    func.count().label("total"),
                    func.count()
                    .filter(T.label_verdict.is_(None))
                    .label("unlabeled"),
                    func.count()
                    .filter(T.label_verdict.isnot(None))
                    .label("labeled_total"),
                    func.count()
                    .filter(
                        T.label_verdict.isnot(None),
                        T.label_verdict == T.llm_label,
                    )
                    .label("matches"),
                ).select_from(T)
            )
        ).one()

        total = int(overall_row.total or 0)
        unlabeled = int(overall_row.unlabeled or 0)
        labeled_total = int(overall_row.labeled_total or 0)
        matches = int(overall_row.matches or 0)

        label_accuracy: float | None = None
        if labeled_total > 0:
            label_accuracy = matches / labeled_total

        # ── Last-100 accuracy ─────────────────────────────────────────────────
        # Only meaningful once 100 rows have been labeled — below that the
        # window denominator would conflate "small corpus" with "fresh trend".
        accuracy_last_100: float | None = None
        if labeled_total >= 100:
            last_100_subq = (
                select(T.label_verdict, T.llm_label)
                .where(T.label_verdict.isnot(None))
                .order_by(T.labeled_at.desc())
                .limit(100)
                .subquery()
            )
            last_100_row = (
                await session.execute(
                    select(
                        func.count().label("last_100_total"),
                        func.count()
                        .filter(
                            last_100_subq.c.label_verdict
                            == last_100_subq.c.llm_label
                        )
                        .label("last_100_matches"),
                    ).select_from(last_100_subq)
                )
            ).one()
            last_100_total = int(last_100_row.last_100_total or 0)
            last_100_matches = int(last_100_row.last_100_matches or 0)
            if last_100_total > 0:
                accuracy_last_100 = last_100_matches / last_100_total

        return StatsResponse(
            total=total,
            unlabeled=unlabeled,
            labeled_total=labeled_total,
            label_accuracy=label_accuracy,
            accuracy_last_100=accuracy_last_100,
        )


# ── Routes ────────────────────────────────────────────────────────────────────
# Literal-path routes (``/next``, ``/stats``) MUST be registered BEFORE the
# parameterized routes (``/{row_id}``, ``/{row_id}/label``) so FastAPI matches
# ``/stats`` to ``get_stats`` rather than routing it to ``get_by_id`` with
# ``row_id="stats"`` (which would 422 on UUID parse).


@router.get("/next", response_model=list[ReviewDspyVariantPr])
async def get_next(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ReviewDspyVariantPrRow]:
    """Return up to ``limit`` unlabeled rows for the operator's queue."""
    store = ReviewDspyVariantPrStore()
    return await store.get_next(session, limit=limit)


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StatsResponse:
    """Return aggregated label statistics for this review kind."""
    store = ReviewDspyVariantPrStore()
    return await store.stats(session)


@router.get("/{row_id}", response_model=ReviewDspyVariantPr)
async def get_by_id(
    row_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReviewDspyVariantPrRow:
    """Return one row by id; 404 when missing."""
    store = ReviewDspyVariantPrStore()
    row = await store.get_by_id(session, row_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"review_dspy_variant_pr {row_id} not found",
        )
    return row


@router.post(
    "/{row_id}/label",
    response_model=ReviewDspyVariantPr,
    status_code=status.HTTP_200_OK,
)
async def label_row(
    row_id: uuid.UUID,
    body: LabelDspyVariantPrRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReviewDspyVariantPrRow:
    """Record an operator verdict + metadata for ``row_id``.

    422 when the operator overrides the LLM recommendation without an
    explanatory ``override_reason`` — the cross-field rule that
    ``build_review_router`` doesn't model (the request body alone cannot
    enforce it because it doesn't carry ``llm_label``).
    """
    store = ReviewDspyVariantPrStore()
    row = await store.get_by_id(session, row_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"review_dspy_variant_pr {row_id} not found",
        )

    if (
        body.label_verdict != row.llm_label
        and body.label_override_reason is None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="override_reason required when overriding the LLM recommendation",
        )

    await store.record_label(
        session,
        row_id,
        label_verdict=body.label_verdict,
        label_notes=body.label_notes,
        label_override_reason=body.label_override_reason,
        labeled_by=body.labeled_by,
    )
    await session.commit()
    await session.refresh(row)
    return row
