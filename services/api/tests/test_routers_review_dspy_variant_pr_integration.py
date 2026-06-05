"""Postgres SQL-correctness tests for the dspy-variant-pr review router.

Gated on ``TREADMILL_INTEGRATION=1`` so the sandbox SKIPs cleanly; CI /
``treadmill-local up`` runs them.  Mirrors ``test_triage_store.py:177-220``
and ``test_models_review_dspy_variant_pr_integration.py``.

These tests round-trip ``ReviewDspyVariantPrStore`` through a real DB so
the SQL semantics (CASE-expression ORDER BY, partial-index WHERE,
COUNT-FILTER math) are pinned at the layer that issues them — the unit
tests above patch the store out, so the SQL is invisible there.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.models.review_dspy_variant_pr import ReviewDspyVariantPrRow
from treadmill_api.routers.review.review_dspy_variant_pr import (
    LABEL_GUIDELINES_VERSION,
    ReviewDspyVariantPrStore,
)


INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
integration = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def async_database_url(database_url: str) -> str:
    return database_url.replace("+psycopg", "+asyncpg")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = sa.create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str) -> None:
    if not INTEGRATION:
        return
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    def _do() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE review_dspy_variant_pr RESTART IDENTITY CASCADE"
                )
            )

    _do()
    yield
    _do()


@pytest_asyncio.fixture
async def session_factory(
    async_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async_engine = create_async_engine(async_database_url)
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    yield factory
    await async_engine.dispose()


def _base_row(**overrides: object) -> dict:
    base: dict = {
        "id": uuid.uuid4(),
        "source_run_id": uuid.uuid4(),
        "source_pr_number": 4321,
        "source_pr_url": "https://github.com/joeLepper/treadmill/pull/4321",
        "judge_role": "role-architect",
        "judge_prompt_path": "treadmill_api/starters/role_architect.md",
        "current_score": Decimal("0.7000"),
        "variant_score": Decimal("0.7543"),
        "improvement": Decimal("0.0543"),
        "patch_diff": "--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-old\n+new\n",
        "corpus_s3_uri": "s3://treadmill-personal/optimizer/runs/x/corpus.jsonl",
        "llm_label": "merge",
        "llm_confidence": "high",
        "llm_rationale": "Higher score, no regressions in spot checks.",
        "llm_prompt_version": "v1.0.0",
        "llm_model": "claude-opus-4-7",
    }
    base.update(overrides)
    return base


@integration
@pytest.mark.asyncio
async def test_get_next_filters_unlabeled_only(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """The partial-index ``WHERE label_verdict IS NULL`` filter excludes
    labeled rows from the queue.
    """
    unlabeled_a = ReviewDspyVariantPrRow(**_base_row(id=uuid.uuid4()))
    unlabeled_b = ReviewDspyVariantPrRow(**_base_row(id=uuid.uuid4()))
    labeled = ReviewDspyVariantPrRow(
        **_base_row(
            id=uuid.uuid4(),
            label_verdict="merge",
            labeled_by="operator",
            labeled_at=datetime(2026, 6, 4, 12, 30, 0, tzinfo=timezone.utc),
        )
    )

    async with session_factory() as session:
        session.add_all([unlabeled_a, unlabeled_b, labeled])
        await session.commit()

    async with session_factory() as session:
        rows = await ReviewDspyVariantPrStore().get_next(session, limit=10)

    ids = {r.id for r in rows}
    assert unlabeled_a.id in ids
    assert unlabeled_b.id in ids
    assert labeled.id not in ids


@integration
@pytest.mark.asyncio
async def test_get_next_orders_low_confidence_first(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """CASE-expression ORDER BY: low → medium → high; ties break on
    ``created_at`` ASC.
    """
    # Created in non-confidence order so we know the ORDER BY did the work.
    high = ReviewDspyVariantPrRow(
        **_base_row(id=uuid.uuid4(), llm_confidence="high")
    )
    medium = ReviewDspyVariantPrRow(
        **_base_row(id=uuid.uuid4(), llm_confidence="medium")
    )
    low = ReviewDspyVariantPrRow(
        **_base_row(id=uuid.uuid4(), llm_confidence="low")
    )

    async with session_factory() as session:
        session.add(high)
        await session.flush()
        session.add(medium)
        await session.flush()
        session.add(low)
        await session.commit()

    async with session_factory() as session:
        rows = await ReviewDspyVariantPrStore().get_next(session, limit=10)

    assert [r.llm_confidence for r in rows] == ["low", "medium", "high"]


@integration
@pytest.mark.asyncio
async def test_get_next_honors_limit(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """``limit`` caps the number of returned rows."""
    rows_in = [
        ReviewDspyVariantPrRow(**_base_row(id=uuid.uuid4())) for _ in range(5)
    ]
    async with session_factory() as session:
        session.add_all(rows_in)
        await session.commit()

    async with session_factory() as session:
        rows = await ReviewDspyVariantPrStore().get_next(session, limit=2)
    assert len(rows) == 2


@integration
@pytest.mark.asyncio
async def test_record_label_stamps_labeled_at_and_guidelines_version(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """``record_label`` writes the four label fields, stamps
    ``labeled_at`` via ``func.now()``, and pins
    ``label_guidelines_version`` to the module constant.
    """
    row_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(ReviewDspyVariantPrRow(**_base_row(id=row_id)))
        await session.commit()

    async with session_factory() as session:
        await ReviewDspyVariantPrStore().record_label(
            session,
            row_id,
            label_verdict="merge",
            label_notes="LGTM",
            label_override_reason=None,
            labeled_by="operator",
        )
        await session.commit()

    async with session_factory() as session:
        fetched = await session.scalar(
            sa.select(ReviewDspyVariantPrRow).where(
                ReviewDspyVariantPrRow.id == row_id
            )
        )

    assert fetched is not None
    assert fetched.label_verdict == "merge"
    assert fetched.label_notes == "LGTM"
    assert fetched.label_override_reason is None
    assert fetched.labeled_by == "operator"
    assert fetched.labeled_at is not None
    assert fetched.label_guidelines_version == LABEL_GUIDELINES_VERSION


@integration
@pytest.mark.asyncio
async def test_stats_label_accuracy_math(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """4 labeled rows, 3 agree with llm_label → ``label_accuracy == 0.75``.

    ``accuracy_last_100`` stays ``None`` because labeled_total < 100.
    """
    base_time = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        # llm_label="merge", agree
        ReviewDspyVariantPrRow(
            **_base_row(
                id=uuid.uuid4(),
                llm_label="merge",
                label_verdict="merge",
                labeled_by="op",
                labeled_at=base_time + timedelta(minutes=1),
            )
        ),
        # llm_label="merge", agree
        ReviewDspyVariantPrRow(
            **_base_row(
                id=uuid.uuid4(),
                llm_label="merge",
                label_verdict="merge",
                labeled_by="op",
                labeled_at=base_time + timedelta(minutes=2),
            )
        ),
        # llm_label="revise", agree
        ReviewDspyVariantPrRow(
            **_base_row(
                id=uuid.uuid4(),
                llm_label="revise",
                label_verdict="revise",
                labeled_by="op",
                labeled_at=base_time + timedelta(minutes=3),
            )
        ),
        # llm_label="merge", disagree
        ReviewDspyVariantPrRow(
            **_base_row(
                id=uuid.uuid4(),
                llm_label="merge",
                label_verdict="drop",
                label_override_reason="lower score on fresh corpus",
                labeled_by="op",
                labeled_at=base_time + timedelta(minutes=4),
            )
        ),
    ]
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()

    async with session_factory() as session:
        stats = await ReviewDspyVariantPrStore().stats(session)

    assert stats.total == 4
    assert stats.unlabeled == 0
    assert stats.labeled_total == 4
    assert stats.label_accuracy is not None
    assert abs(stats.label_accuracy - 0.75) < 1e-9
    assert stats.accuracy_last_100 is None  # labeled_total < 100


@integration
@pytest.mark.asyncio
async def test_stats_returns_zero_when_empty(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """Empty table → zero counts and null accuracies."""
    async with session_factory() as session:
        stats = await ReviewDspyVariantPrStore().stats(session)
    assert stats.total == 0
    assert stats.unlabeled == 0
    assert stats.labeled_total == 0
    assert stats.label_accuracy is None
    assert stats.accuracy_last_100 is None
