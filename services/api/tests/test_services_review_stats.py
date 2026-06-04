"""Unit tests for ``compute_stats`` (ADR-0070 substep 1.2).

Exercises ``services/review_stats.py`` independent of the HTTP layer.
Uses the same synthetic ``_FakeKindRow`` subclass (distinct tablename) plus
a stub async session that returns scripted scalar values per query — no
live Postgres.  Test functions are ``async def`` so pytest-asyncio
(``asyncio_mode = "auto"``) handles the event loop.

Coverage:
  Case 1 — empty table: all zeros, both accuracy fields None.
  Case 2 — mixed: 10 rows, 4 unlabeled, 6 labeled where 5 match LLM.
  Case 3 — fewer than 100 labeled rows: accuracy_last_100 uses actual
            row count as denominator (12 labeled, 9 match → 9/12).
  Case 4 — NULL operator verdict rows are excluded from the accuracy
            numerator AND denominator (they're already in ``unlabeled``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Text

from treadmill_api.database import Base
from treadmill_api.models.review_queue import ReviewQueueRowMixin
from treadmill_api.services.review_stats import StatsResponse, compute_stats

# ── Synthetic kind — distinct tablename from mixin-test and router-test ────────

_FAKE_TABLE = "_fake_review_kind_stats_test"


class _FakeStatsKindRow(ReviewQueueRowMixin, Base):
    """Synthetic subclass for stats-service unit tests."""

    __tablename__ = _FAKE_TABLE

    llm_label: Mapped[str] = mapped_column(Text, nullable=False)
    label_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        *ReviewQueueRowMixin.review_queue_check_constraints(table_name=_FAKE_TABLE),
        ReviewQueueRowMixin.unlabeled_index(
            table_name=_FAKE_TABLE, verdict_column="label_verdict"
        ),
    )


# ── Stub session ──────────────────────────────────────────────────────────────


class _StubSession:
    """Stub that returns scripted scalars in queue order.

    ``compute_stats`` calls ``session.scalar()`` several times.  Each call
    pops the next value from ``_scalar_queue`` so tests can fully control
    what the function sees.
    """

    def __init__(self, scalar_queue: list[Any]) -> None:
        self._scalar_queue = list(scalar_queue)

    async def scalar(self, _stmt: Any) -> Any:
        if self._scalar_queue:
            return self._scalar_queue.pop(0)
        return 0

    async def execute(self, _stmt: Any) -> Any:  # pragma: no cover
        raise AssertionError("compute_stats must not call execute() — use scalar()")


# ── Case 1: empty table ───────────────────────────────────────────────────────


async def test_empty_table_returns_all_zeros_and_none_accuracy() -> None:
    """Empty table → total=0, unlabeled=0, labeled_total=0, accuracy=None."""
    session = _StubSession(scalar_queue=[0, 0])
    result = await compute_stats(
        session,  # type: ignore[arg-type]
        row_cls=_FakeStatsKindRow,
        verdict_attr="label_verdict",
    )
    assert isinstance(result, StatsResponse)
    assert result.total == 0
    assert result.unlabeled == 0
    assert result.labeled_total == 0
    assert result.label_accuracy is None
    assert result.accuracy_last_100 is None


# ── Case 2: mixed table ───────────────────────────────────────────────────────


async def test_mixed_table_computes_accuracy_fraction() -> None:
    """10 rows, 4 unlabeled, 6 labeled where 5 match LLM.

    Expected:
      total=10, unlabeled=4, labeled_total=6
      label_accuracy = 5/6 ≈ 0.8333
      accuracy_last_100 = 5/6 (only 6 labeled → denominator=6)
    """
    # scalar() sequence:
    #   1. total=10
    #   2. unlabeled=4  → labeled_total=6
    #   3. match_count=5  → label_accuracy = 5/6
    #   4. last_100_match=5  → accuracy_last_100 = 5/min(6,100) = 5/6
    session = _StubSession(scalar_queue=[10, 4, 5, 5])
    result = await compute_stats(
        session,  # type: ignore[arg-type]
        row_cls=_FakeStatsKindRow,
        verdict_attr="label_verdict",
    )
    assert result.total == 10
    assert result.unlabeled == 4
    assert result.labeled_total == 6
    assert result.label_accuracy is not None
    assert abs(result.label_accuracy - 5 / 6) < 1e-9
    assert result.accuracy_last_100 is not None
    assert abs(result.accuracy_last_100 - 5 / 6) < 1e-9


# ── Case 3: fewer than 100 labeled rows ──────────────────────────────────────


async def test_fewer_than_100_labeled_uses_actual_count_as_denominator() -> None:
    """12 labeled rows, 9 match → accuracy_last_100 = 9/12, NOT 9/100."""
    # scalar() sequence:
    #   1. total=12
    #   2. unlabeled=0  → labeled_total=12
    #   3. match_count=9  → label_accuracy = 9/12 = 0.75
    #   4. last_100_match=9  → accuracy_last_100 = 9/min(12,100) = 9/12 = 0.75
    session = _StubSession(scalar_queue=[12, 0, 9, 9])
    result = await compute_stats(
        session,  # type: ignore[arg-type]
        row_cls=_FakeStatsKindRow,
        verdict_attr="label_verdict",
    )
    assert result.labeled_total == 12
    assert result.label_accuracy is not None
    assert abs(result.label_accuracy - 9 / 12) < 1e-9
    assert result.accuracy_last_100 is not None
    # Must be 9/12, not 9/100.
    assert abs(result.accuracy_last_100 - 9 / 12) < 1e-9
    assert result.accuracy_last_100 != 9 / 100


# ── Case 4: NULL operator verdict excluded ────────────────────────────────────


async def test_null_operator_verdict_excluded_from_accuracy() -> None:
    """Rows where the operator's verdict IS NULL are counted as unlabeled.

    They are therefore excluded from both the accuracy numerator and
    denominator because labeled_total is derived as total - unlabeled
    (unlabeled = verdict IS NULL), and the accuracy queries filter on
    verdict IS NOT NULL.

    Scenario: 5 total rows, 2 have NULL verdict (unlabeled), 3 labeled.
    Of the 3 labeled rows, 2 agree with the LLM.
    label_accuracy = 2/3.
    """
    # scalar() sequence:
    #   1. total=5
    #   2. unlabeled=2  → labeled_total=3
    #   3. match_count=2  → label_accuracy = 2/3
    #   4. last_100_match=2  → accuracy_last_100 = 2/min(3,100) = 2/3
    session = _StubSession(scalar_queue=[5, 2, 2, 2])
    result = await compute_stats(
        session,  # type: ignore[arg-type]
        row_cls=_FakeStatsKindRow,
        verdict_attr="label_verdict",
    )
    assert result.total == 5
    assert result.unlabeled == 2
    assert result.labeled_total == 3
    assert result.label_accuracy is not None
    assert abs(result.label_accuracy - 2 / 3) < 1e-9
    assert result.accuracy_last_100 is not None
    assert abs(result.accuracy_last_100 - 2 / 3) < 1e-9


# ── StatsResponse model ───────────────────────────────────────────────────────


def test_stats_response_model_validation() -> None:
    """``StatsResponse`` round-trips through Pydantic cleanly."""
    sr = StatsResponse(
        total=10,
        unlabeled=4,
        labeled_total=6,
        label_accuracy=0.8,
        accuracy_last_100=None,
    )
    assert sr.total == 10
    assert sr.labeled_total == 6
    assert abs(sr.label_accuracy - 0.8) < 1e-9
    assert sr.accuracy_last_100 is None

    dumped = sr.model_dump()
    rebuilt = StatsResponse(**dumped)
    assert rebuilt == sr


def test_stats_response_none_accuracy_serializes_as_null() -> None:
    """Both accuracy fields serialize to ``null`` in JSON when ``None``."""
    sr = StatsResponse(
        total=0,
        unlabeled=0,
        labeled_total=0,
        label_accuracy=None,
        accuracy_last_100=None,
    )
    j = sr.model_dump()
    assert j["label_accuracy"] is None
    assert j["accuracy_last_100"] is None
