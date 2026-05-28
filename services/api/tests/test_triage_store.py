"""Tests for the ADR-0061 triage_findings persistence layer.

A non-DB structural check runs always; round-trip checks against real
Postgres are gated on ``TREADMILL_INTEGRATION=1`` (same pattern as
``tests/test_onboarding_store.py``).
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.models.triage_finding import TriageFindingRow
from treadmill_api.schemas.triage_finding import TriageFinding
from treadmill_api.triage_store import TriageStore


# ── Non-DB structural tests (always run) ──────────────────────────────────────


def test_triage_models_and_store_shape() -> None:
    """Model maps to the correct table; store exposes the four required methods.

    Runs without a database — pure import + attribute check.
    """
    assert TriageFindingRow.__tablename__ == "triage_findings"

    for method in (
        "insert_finding",
        "update_outcome",
        "record_label",
        "get_unlabeled_findings",
    ):
        assert hasattr(TriageStore, method), f"TriageStore missing {method!r}"


def test_suppression_signal_required_when_suppressed() -> None:
    """Pydantic rejects dispatch_action='suppressed' with no suppression_signal."""
    with pytest.raises(ValidationError, match="suppression_signal"):
        TriageFinding(
            **_base_finding(dispatch_action="suppressed", suppression_signal=None)
        )


def test_suppression_signal_forbidden_when_not_suppressed() -> None:
    """Pydantic rejects suppression_signal set when dispatch_action != 'suppressed'."""
    with pytest.raises(ValidationError, match="suppression_signal"):
        TriageFinding(
            **_base_finding(
                dispatch_action="research_only",
                suppression_signal="low_confidence",
            )
        )


def test_dispatched_plan_id_required_when_dispatched() -> None:
    """Pydantic rejects dispatch_action='dispatched' with no dispatched_plan_id."""
    with pytest.raises(ValidationError, match="dispatched_plan_id"):
        TriageFinding(
            **_base_finding(dispatch_action="dispatched", dispatched_plan_id=None)
        )


def test_dispatched_plan_id_forbidden_when_not_dispatched() -> None:
    """Pydantic rejects dispatched_plan_id set when dispatch_action != 'dispatched'."""
    with pytest.raises(ValidationError, match="dispatched_plan_id"):
        TriageFinding(
            **_base_finding(
                dispatch_action="research_only",
                dispatched_plan_id=uuid.uuid4(),
            )
        )


def test_all_category_values_accepted() -> None:
    """All nine category literals (including 'other') are valid."""
    categories = [
        "console_error",
        "network_failure",
        "broken_asset",
        "accessibility",
        "layout_overflow",
        "consistency",
        "dead_affordance",
        "loading_state",
        "other",
    ]
    for cat in categories:
        f = TriageFinding(**_base_finding(category=cat))  # type: ignore[arg-type]
        assert f.category == cat


def test_unknown_category_rejected() -> None:
    """An unknown category value raises a Pydantic ValidationError."""
    with pytest.raises(ValidationError, match="category"):
        TriageFinding(**_base_finding(category="performance"))  # type: ignore[arg-type]


def test_observation_max_length_enforced() -> None:
    """observation must not exceed 240 characters."""
    with pytest.raises(ValidationError, match="observation"):
        TriageFinding(**_base_finding(observation="x" * 241))


def test_proposed_resolution_max_length_enforced() -> None:
    """proposed_resolution must not exceed 900 characters."""
    with pytest.raises(ValidationError, match="proposed_resolution"):
        TriageFinding(**_base_finding(proposed_resolution="x" * 901))


# ── Helpers ───────────────────────────────────────────────────────────────────

_RUN_ID = uuid.UUID("00000000-1111-4000-8000-000000000001")
_TARGET_URL = "http://localhost:5174/"


def _base_finding(**overrides: object) -> dict:
    """Minimal valid TriageFinding kwargs; overrides replace specific fields."""
    base: dict = {
        "finding_id": uuid.uuid4(),
        "run_id": _RUN_ID,
        "prompt_version": "v1.0.0",
        "model": "claude-opus-4-7",
        "mode": "periodic",
        "target_url": _TARGET_URL,
        "viewport_w": 1440,
        "viewport_h": 900,
        "git_sha": "abc1234",
        "screenshot_uri": "s3://corpus/triage/runs/test/01/screen.png",
        "console_log_uri": "s3://corpus/triage/runs/test/01/console.log",
        "network_log_uri": "s3://corpus/triage/runs/test/01/network.log",
        "evidence_summary": {
            "console_errors": 0,
            "http_4xx": 0,
            "http_5xx": 0,
            "requestfailed": 0,
        },
        "category": "layout_overflow",
        "severity": "medium",
        "confidence": "high",
        "observation": "Escalation strip occupies full viewport, hiding bucket headers.",
        "evidence_pointer": "screen.png:y=80-900",
        "proposed_resolution": "Cap escalation strip at max-height: 240px with overflow-y: auto.",
        "dispatch_action": "research_only",
        "dispatch_reason": "Severity medium, confidence high — research path.",
    }
    base.update(overrides)
    return base


def _suppressed_finding(**overrides: object) -> dict:
    """A valid suppressed finding."""
    return _base_finding(
        dispatch_action="suppressed",
        suppression_signal="design_intent",
        **overrides,
    )


# ── Integration round-trips ───────────────────────────────────────────────────

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
                    "TRUNCATE TABLE triage_findings RESTART IDENTITY CASCADE"
                )
            )

    _do()
    yield
    _do()


@pytest.fixture
def truncate_with_plans(engine: Engine) -> Iterator[None]:
    """Truncate triage_findings and plans (FK dependency for dispatched rows)."""

    def _do() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE triage_findings, plans RESTART IDENTITY CASCADE"
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


@integration
@pytest.mark.asyncio
async def test_insert_finding_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """insert_finding persists all fields; get_unlabeled_findings returns them."""
    store = TriageStore()
    finding = TriageFinding(**_suppressed_finding(finding_id=uuid.uuid4()))

    async with session_factory() as session:
        returned_id = await store.insert_finding(session, finding)
        await session.commit()

    assert returned_id == finding.finding_id

    async with session_factory() as session:
        unlabeled = await store.get_unlabeled_findings(session, limit=10)

    assert len(unlabeled) == 1
    fetched = unlabeled[0]
    assert fetched.finding_id == finding.finding_id
    assert fetched.run_id == finding.run_id
    assert fetched.category == "layout_overflow"
    assert fetched.dispatch_action == "suppressed"
    assert fetched.suppression_signal == "design_intent"
    assert fetched.label_is_real_bug is None


@integration
@pytest.mark.asyncio
async def test_update_outcome_no_match_returns_zero(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """update_outcome returns 0 when no finding has the given dispatched_plan_id."""
    store = TriageStore()
    async with session_factory() as session:
        count = await store.update_outcome(
            session,
            dispatched_plan_id=uuid.uuid4(),
            outcome_state="merged",
            outcome_pr_number=99,
            outcome_merged_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc),
        )
        await session.commit()
    assert count == 0


@integration
@pytest.mark.asyncio
async def test_update_outcome_match_and_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    engine: Engine,
    truncate_with_plans: None,
) -> None:
    """update_outcome updates the matching row; re-running with same args is a no-op."""
    store = TriageStore()

    # Create a minimal plan so the FK on dispatched_plan_id is satisfied.
    plan_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO plans (id, repo, created_by) "
                "VALUES (:id, 'test/repo', 'test')"
            ),
            {"id": str(plan_id)},
        )

    finding = TriageFinding(
        **_base_finding(
            finding_id=uuid.uuid4(),
            dispatch_action="dispatched",
            dispatched_plan_id=plan_id,
        )
    )
    async with session_factory() as session:
        await store.insert_finding(session, finding)
        await session.commit()

    merged_at = datetime(2026, 5, 28, 14, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        count = await store.update_outcome(
            session,
            dispatched_plan_id=plan_id,
            outcome_state="merged",
            outcome_pr_number=49,
            outcome_merged_at=merged_at,
        )
        await session.commit()
    assert count == 1

    # Re-running with the same args is idempotent (no error, count still 1).
    async with session_factory() as session:
        count2 = await store.update_outcome(
            session,
            dispatched_plan_id=plan_id,
            outcome_state="merged",
            outcome_pr_number=49,
            outcome_merged_at=merged_at,
        )
        await session.commit()
    assert count2 == 1

    # Verify the values were actually written.
    async with session_factory() as session:
        row = await session.scalar(
            sa.select(TriageFindingRow).where(
                TriageFindingRow.finding_id == finding.finding_id
            )
        )
    assert row is not None
    assert row.outcome_state == "merged"
    assert row.outcome_pr_number == 49


@integration
@pytest.mark.asyncio
async def test_get_unlabeled_excludes_labeled_rows(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """get_unlabeled_findings excludes rows that have label_is_real_bug set."""
    store = TriageStore()

    unlabeled_finding = TriageFinding(**_suppressed_finding(finding_id=uuid.uuid4()))
    labeled_finding = TriageFinding(**_suppressed_finding(finding_id=uuid.uuid4()))

    async with session_factory() as session:
        await store.insert_finding(session, unlabeled_finding)
        await store.insert_finding(session, labeled_finding)
        await session.commit()

    async with session_factory() as session:
        await store.record_label(
            session,
            finding_id=labeled_finding.finding_id,
            label_is_real_bug=False,
            label_dispatch_action="suppressed",
            labeled_by="operator",
        )
        await session.commit()

    async with session_factory() as session:
        unlabeled = await store.get_unlabeled_findings(session, limit=50)

    ids = {f.finding_id for f in unlabeled}
    assert unlabeled_finding.finding_id in ids
    assert labeled_finding.finding_id not in ids


@integration
@pytest.mark.asyncio
async def test_record_label_sets_fields_and_labeled_at(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """record_label persists all label fields and sets labeled_at."""
    store = TriageStore()
    finding = TriageFinding(**_suppressed_finding(finding_id=uuid.uuid4()))

    async with session_factory() as session:
        await store.insert_finding(session, finding)
        await session.commit()

    async with session_factory() as session:
        await store.record_label(
            session,
            finding_id=finding.finding_id,
            label_is_real_bug=False,
            label_severity="low",
            label_category="layout_overflow",
            label_fix_in_dsl=True,
            label_dispatch_action="suppressed",
            label_notes="Intentional per DESIGN.md terminal density.",
            labeled_by="operator",
            label_guidelines_version="v1",
        )
        await session.commit()

    async with session_factory() as session:
        row = await session.scalar(
            sa.select(TriageFindingRow).where(
                TriageFindingRow.finding_id == finding.finding_id
            )
        )

    assert row is not None
    assert row.label_is_real_bug is False
    assert row.label_severity == "low"
    assert row.label_category == "layout_overflow"
    assert row.label_fix_in_dsl is True
    assert row.label_dispatch_action == "suppressed"
    assert row.label_notes == "Intentional per DESIGN.md terminal density."
    assert row.labeled_by == "operator"
    assert row.label_guidelines_version == "v1"
    assert row.labeled_at is not None
