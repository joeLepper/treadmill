"""Postgres round-trip tests for the ADR-0070 review_dspy_variant_pr model.

Gated on ``TREADMILL_INTEGRATION=1`` so the sandbox SKIPs cleanly; CI /
``treadmill-local up`` runs them. Fixture shape mirrors
``test_triage_store.py``.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.models.review_dspy_variant_pr import ReviewDspyVariantPrRow


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
async def test_insert_round_trip_persists_six_layers(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """Insert a complete row and read back every layer."""
    row_kwargs = _base_row(
        label_verdict="merge",
        label_notes="LGTM",
        labeled_by="operator",
        labeled_at=datetime(2026, 6, 4, 12, 30, 0, tzinfo=timezone.utc),
        label_guidelines_version="v1",
        outcome_state="pending",
    )
    row = ReviewDspyVariantPrRow(**row_kwargs)

    async with session_factory() as session:
        session.add(row)
        await session.commit()

    async with session_factory() as session:
        fetched = await session.scalar(
            sa.select(ReviewDspyVariantPrRow).where(
                ReviewDspyVariantPrRow.id == row_kwargs["id"]
            )
        )

    assert fetched is not None
    # Provenance
    assert fetched.id == row_kwargs["id"]
    assert fetched.source_run_id == row_kwargs["source_run_id"]
    assert fetched.source_pr_number == 4321
    assert fetched.source_pr_url.endswith("/4321")
    assert fetched.created_at is not None  # server_default
    # Candidate content
    assert fetched.judge_role == "role-architect"
    assert fetched.judge_prompt_path.endswith("role_architect.md")
    assert fetched.current_score == Decimal("0.7000")
    assert fetched.variant_score == Decimal("0.7543")
    assert fetched.improvement == Decimal("0.0543")
    assert "+new" in fetched.patch_diff
    assert fetched.corpus_s3_uri.startswith("s3://")
    # LLM recommendation
    assert fetched.llm_label == "merge"
    assert fetched.llm_confidence == "high"
    assert fetched.llm_rationale.startswith("Higher score")
    assert fetched.llm_prompt_version == "v1.0.0"
    assert fetched.llm_model == "claude-opus-4-7"
    # Operator label
    assert fetched.label_verdict == "merge"
    assert fetched.label_notes == "LGTM"
    assert fetched.label_override_reason is None
    # Labeled metadata
    assert fetched.labeled_by == "operator"
    assert fetched.labeled_at is not None
    assert fetched.label_guidelines_version == "v1"
    # Outcome
    assert fetched.outcome_state == "pending"
    assert fetched.outcome_merged_at is None


@integration
@pytest.mark.asyncio
async def test_llm_label_check_constraint_rejects_invalid(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """An ``llm_label`` outside the allowed set raises IntegrityError."""
    row = ReviewDspyVariantPrRow(**_base_row(llm_label="bogus"))
    async with session_factory() as session:
        session.add(row)
        with pytest.raises(IntegrityError, match="ck_review_dspy_variant_pr_llm_label"):
            await session.commit()


@integration
@pytest.mark.asyncio
async def test_label_verdict_check_constraint_rejects_invalid(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """A non-null ``label_verdict`` outside the allowed set raises IntegrityError."""
    row = ReviewDspyVariantPrRow(**_base_row(label_verdict="bogus"))
    async with session_factory() as session:
        session.add(row)
        with pytest.raises(
            IntegrityError, match="ck_review_dspy_variant_pr_label_verdict"
        ):
            await session.commit()


@integration
@pytest.mark.asyncio
async def test_unlabeled_partial_index_query_returns_only_nulls(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """The partial index's predicate ``label_verdict IS NULL`` filters correctly."""
    unlabeled = ReviewDspyVariantPrRow(**_base_row(id=uuid.uuid4()))
    labeled = ReviewDspyVariantPrRow(
        **_base_row(
            id=uuid.uuid4(),
            label_verdict="merge",
            labeled_by="operator",
            labeled_at=datetime(2026, 6, 4, 12, 30, 0, tzinfo=timezone.utc),
        )
    )

    async with session_factory() as session:
        session.add(unlabeled)
        session.add(labeled)
        await session.commit()

    async with session_factory() as session:
        result = await session.scalars(
            sa.select(ReviewDspyVariantPrRow).where(
                ReviewDspyVariantPrRow.label_verdict.is_(None)
            )
        )
        rows = list(result)

    ids = {r.id for r in rows}
    assert unlabeled.id in ids
    assert labeled.id not in ids
