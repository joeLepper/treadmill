"""Aggregated stats for an ADR-0070 review-queue table.

``compute_stats`` is called by the ``GET /stats`` endpoint that
``build_review_router`` attaches to every per-kind router.  All queries go
through the SQLAlchemy ORM — no raw SQL strings.
"""

from __future__ import annotations

from typing import Any, Callable, Union

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel

LlmLabelArg = Union[str, Callable[[type], Any]]


class StatsResponse(BaseModel):
    """Wire shape returned by ``GET /<kind>/stats``."""

    total: int
    unlabeled: int
    labeled_total: int
    label_accuracy: float | None
    accuracy_last_100: float | None


async def compute_stats(
    session: AsyncSession,
    *,
    row_cls: type,
    verdict_attr: str,
    llm_label_attr: LlmLabelArg = "llm_label",
    id_attr: str = "id",
) -> StatsResponse:
    """Compute labeling statistics for one review-queue kind.

    Parameters
    ----------
    session:
        Active async SQLAlchemy session.
    row_cls:
        SQLAlchemy declarative class (subclass of ReviewQueueRowMixin + Base).
    verdict_attr:
        Name of the column holding the operator's verdict (nullable until
        labeled).
    llm_label_attr:
        Either the name of the column holding the LLM's recommendation, OR a
        callable taking ``row_cls`` and returning a SQL expression (used by
        legacy tables that lack a native ``llm_label`` column — e.g. ADR-0061
        ``TriageFindingRow`` derives it from ``confidence``).  Compared
        against the operator verdict to compute agreement.
    id_attr:
        Name of the primary-key attribute on ``row_cls`` (default ``"id"``).
        Used by the last-100 subquery; ADR-0061 ``TriageFindingRow`` passes
        ``id_attr="finding_id"``.

    Notes
    -----
    * ``label_accuracy`` and ``accuracy_last_100`` are ``None`` when
      ``labeled_total == 0`` — there is no denominator.
    * ``accuracy_last_100`` uses the actual count of labeled rows in the
      look-back window as its denominator, not a hard-coded 100, so the
      fraction is honest when the corpus is still small.
    * NULL operator verdicts are implicitly excluded from both accuracy
      numerator and denominator because we filter on ``verdict IS NOT NULL``
      for both counts.
    """
    verdict_col = getattr(row_cls, verdict_attr)
    llm_col = (
        llm_label_attr(row_cls) if callable(llm_label_attr)
        else getattr(row_cls, llm_label_attr)
    )
    labeled_at_col = getattr(row_cls, "labeled_at")
    id_col = getattr(row_cls, id_attr)

    # ── Count total rows ──────────────────────────────────────────────────────
    total: int = await session.scalar(
        select(func.count()).select_from(row_cls)
    ) or 0

    # ── Count unlabeled rows (verdict IS NULL) ────────────────────────────────
    unlabeled: int = await session.scalar(
        select(func.count()).select_from(row_cls).where(verdict_col.is_(None))
    ) or 0

    labeled_total = total - unlabeled

    # ── Overall accuracy ──────────────────────────────────────────────────────
    label_accuracy: float | None = None
    if labeled_total > 0:
        match_count: int = await session.scalar(
            select(func.count())
            .select_from(row_cls)
            .where(verdict_col.isnot(None))
            .where(verdict_col == llm_col)
        ) or 0
        label_accuracy = match_count / labeled_total

    # ── Last-100 accuracy ─────────────────────────────────────────────────────
    accuracy_last_100: float | None = None
    if labeled_total > 0:
        # Inner SELECT: 100 most recently labeled row IDs.  Passed as a Select
        # directly to .in_() rather than wrapping in a named subquery to
        # sidestep column-key mismatches when id_attr != "id" — SQLAlchemy 2.0
        # accepts a Select in .in_() and generates a correlated IN clause.
        last_100_ids = (
            select(id_col)
            .where(verdict_col.isnot(None))
            .order_by(labeled_at_col.desc())
            .limit(100)
        )

        # Count how many of those 100 rows agree with the LLM label.
        last_100_match: int = await session.scalar(
            select(func.count())
            .select_from(row_cls)
            .where(id_col.in_(last_100_ids))
            .where(verdict_col == llm_col)
        ) or 0

        # Denominator is the actual window size (≤ 100).
        denominator = min(labeled_total, 100)
        accuracy_last_100 = last_100_match / denominator

    return StatsResponse(
        total=total,
        unlabeled=unlabeled,
        labeled_total=labeled_total,
        label_accuracy=label_accuracy,
        accuracy_last_100=accuracy_last_100,
    )
