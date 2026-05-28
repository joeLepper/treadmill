"""Integration tests for the ADR-0061 triage_findings outcome projection
in the coordination consumer.

When a dispatched plan's PR merges (or its task is cancelled / superseded),
the consumer projects the verb onto the corresponding triage_findings row
inside the same transaction as the source event. No sweeper, no race
window — the projection rides the same idempotency guarantees as
``task_status`` / ``task_mergeability``.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest \\
      services/api/tests/test_consumer_triage_outcome.py
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
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

from treadmill_api.coordination import CoordinationConsumer
from treadmill_api.schemas.triage_finding import TriageFinding
from treadmill_api.triage_store import TriageStore

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)

_TEST_TABLES = (
    "triage_findings",
    "events",
    "workflow_run_steps",
    "workflow_runs",
    "task_prs",
    "task_dependencies",
    "tasks",
    "plans",
    "workflow_version_steps",
    "workflow_versions",
    "workflows",
)

_REPO = "test/triage-outcome"
_PR_NUMBER = 99
_BRANCH = "task/aaaaaaaa-triage-fix"


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
                    "TRUNCATE TABLE "
                    + ", ".join(_TEST_TABLES)
                    + " RESTART IDENTITY CASCADE"
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


def _dispatched_finding_kwargs(dispatched_plan_id: uuid.UUID) -> dict:
    """Minimal valid TriageFinding kwargs with ``dispatch_action='dispatched'``
    pointing at the given plan id (the schema enforces the pairing)."""
    return {
        "finding_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "prompt_version": "v1.0.0",
        "model": "claude-opus-4-7",
        "mode": "periodic",
        "target_url": "http://localhost:5174/",
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
        "severity": "high",
        "confidence": "high",
        "observation": "Escalation strip occupies full viewport.",
        "evidence_pointer": "screen.png:y=80-900",
        "proposed_resolution": "Cap escalation strip at max-height: 240px.",
        "dispatch_action": "dispatched",
        "dispatch_reason": "Severity high, confidence high — dispatched.",
        "dispatched_plan_id": dispatched_plan_id,
    }


def _seed_plan_task_pr(
    engine: Engine,
    *,
    repo: str = _REPO,
    pr_number: int = _PR_NUMBER,
    branch: str = _BRANCH,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Plan + Task + task_prs row. Returns (plan_id, task_id)."""
    with engine.begin() as conn:
        plan_id = conn.execute(
            sa.text("INSERT INTO plans (repo) VALUES (:repo) RETURNING id"),
            {"repo": repo},
        ).scalar()
        task_id = conn.execute(
            sa.text(
                "INSERT INTO tasks (plan_id, repo, title) "
                "VALUES (:p, :repo, 't') RETURNING id"
            ),
            {"p": plan_id, "repo": repo},
        ).scalar()
        conn.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id, branch) "
                "VALUES (:repo, :pr, :t, :b)"
            ),
            {"repo": repo, "pr": pr_number, "t": task_id, "b": branch},
        )
    return plan_id, task_id


def _seed_plan_task(
    engine: Engine, *, repo: str = _REPO
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Plan + Task (no task_prs)."""
    with engine.begin() as conn:
        plan_id = conn.execute(
            sa.text("INSERT INTO plans (repo) VALUES (:repo) RETURNING id"),
            {"repo": repo},
        ).scalar()
        task_id = conn.execute(
            sa.text(
                "INSERT INTO tasks (plan_id, repo, title) "
                "VALUES (:p, :repo, 't') RETURNING id"
            ),
            {"p": plan_id, "repo": repo},
        ).scalar()
    return plan_id, task_id


# ── pr_merged → outcome_state='merged' ────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_merged_projects_merged_outcome_with_pr_and_merged_at(
    session_factory: async_sessionmaker[AsyncSession],
    engine: Engine,
    truncate: None,
) -> None:
    """A pr_merged event for a task whose plan has a triage_findings row
    updates ``outcome_state='merged'`` + ``outcome_pr_number`` from the
    payload + ``outcome_merged_at`` from the persisted event's
    ``created_at``."""
    plan_id, _ = _seed_plan_task_pr(engine)

    finding = TriageFinding(
        **_dispatched_finding_kwargs(plan_id)
    )
    async with session_factory() as session:
        await TriageStore().insert_finding(session, finding)
        await session.commit()

    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory,
    )
    await consumer.handle({
        "entity_type": "github",
        "action": "pr_merged",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": _REPO,
            "pr_number": _PR_NUMBER,
            "sender": "alice",
            "merged_sha": "deadbeef" * 5,
        },
    })

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT outcome_state, outcome_pr_number, outcome_merged_at "
                "FROM triage_findings WHERE finding_id = :id"
            ),
            {"id": finding.finding_id},
        ).one()
    assert row.outcome_state == "merged"
    assert row.outcome_pr_number == _PR_NUMBER
    assert row.outcome_merged_at is not None


# ── task.cancelled → outcome_state='cancelled' ────────────────────────────────


@pytest.mark.asyncio
async def test_task_cancelled_projects_cancelled_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    engine: Engine,
    truncate: None,
) -> None:
    """A task.cancelled event whose task lives on a plan with a
    triage_findings row updates ``outcome_state='cancelled'``."""
    plan_id, task_id = _seed_plan_task(engine)
    finding = TriageFinding(
        **_dispatched_finding_kwargs(plan_id)
    )
    async with session_factory() as session:
        await TriageStore().insert_finding(session, finding)
        await session.commit()

    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory,
    )
    await consumer.handle({
        "entity_type": "task",
        "action": "cancelled",
        "event_id": str(uuid.uuid4()),
        "task_id": str(task_id),
        "payload": {"reason": "operator cancel"},
    })

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT outcome_state, outcome_pr_number, outcome_merged_at "
                "FROM triage_findings WHERE finding_id = :id"
            ),
            {"id": finding.finding_id},
        ).one()
    assert row.outcome_state == "cancelled"
    assert row.outcome_pr_number is None
    assert row.outcome_merged_at is None


# ── task.superseded → outcome_state='superseded' ─────────────────────────────


@pytest.mark.asyncio
async def test_task_superseded_projects_superseded_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    engine: Engine,
    truncate: None,
) -> None:
    """A task.superseded event whose task lives on a plan with a
    triage_findings row updates ``outcome_state='superseded'``."""
    plan_id, task_id = _seed_plan_task(engine)
    finding = TriageFinding(
        **_dispatched_finding_kwargs(plan_id)
    )
    async with session_factory() as session:
        await TriageStore().insert_finding(session, finding)
        await session.commit()

    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory,
    )
    await consumer.handle({
        "entity_type": "task",
        "action": "superseded",
        "event_id": str(uuid.uuid4()),
        "task_id": str(task_id),
        "payload": {"superseded_by_task_id": str(uuid.uuid4())},
    })

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT outcome_state, outcome_pr_number, outcome_merged_at "
                "FROM triage_findings WHERE finding_id = :id"
            ),
            {"id": finding.finding_id},
        ).one()
    assert row.outcome_state == "superseded"
    assert row.outcome_pr_number is None
    assert row.outcome_merged_at is None


# ── pr_merged with no matching triage_findings row → no-op ────────────────────


@pytest.mark.asyncio
async def test_pr_merged_no_matching_finding_is_no_op(
    session_factory: async_sessionmaker[AsyncSession],
    engine: Engine,
    truncate: None,
) -> None:
    """A pr_merged event for a task whose plan has no triage_findings row
    is a clean no-op — the handler doesn't error and no row mutates."""
    _seed_plan_task_pr(engine)

    # Plant a triage_findings row on an UNRELATED plan so we can prove
    # nothing changes on it either.
    other_plan_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO plans (id, repo, created_by) "
                "VALUES (:id, 'other/repo', 'test')"
            ),
            {"id": str(other_plan_id)},
        )
    other_finding = TriageFinding(
        **_dispatched_finding_kwargs(other_plan_id)
    )
    async with session_factory() as session:
        await TriageStore().insert_finding(session, other_finding)
        await session.commit()

    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory,
    )
    await consumer.handle({
        "entity_type": "github",
        "action": "pr_merged",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": _REPO,
            "pr_number": _PR_NUMBER,
            "sender": "alice",
            "merged_sha": "deadbeef" * 5,
        },
    })

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT outcome_state FROM triage_findings "
                "WHERE finding_id = :id"
            ),
            {"id": other_finding.finding_id},
        ).one()
    assert row.outcome_state is None


# ── pr_merged re-projection is idempotent ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_merged_re_projection_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    engine: Engine,
    truncate: None,
) -> None:
    """Re-delivering the same pr_merged event (same event_id, same payload)
    does not mutate the triage_findings row beyond the first projection.

    Stability invariant: we read ``outcome_merged_at`` off the persisted
    Event row's ``created_at``, which is stable across replays. So the
    second call writes the same value as the first."""
    plan_id, _ = _seed_plan_task_pr(engine)
    finding = TriageFinding(
        **_dispatched_finding_kwargs(plan_id)
    )
    async with session_factory() as session:
        await TriageStore().insert_finding(session, finding)
        await session.commit()

    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory,
    )
    event_id = str(uuid.uuid4())
    message = {
        "entity_type": "github",
        "action": "pr_merged",
        "event_id": event_id,
        "payload": {
            "repo": _REPO,
            "pr_number": _PR_NUMBER,
            "sender": "alice",
            "merged_sha": "deadbeef" * 5,
        },
    }

    await consumer.handle(message)
    with engine.connect() as conn:
        first = conn.execute(
            sa.text(
                "SELECT outcome_state, outcome_pr_number, outcome_merged_at "
                "FROM triage_findings WHERE finding_id = :id"
            ),
            {"id": finding.finding_id},
        ).one()
    assert first.outcome_state == "merged"
    first_merged_at = first.outcome_merged_at

    # Same record (same event_id) replayed — the ON CONFLICT DO NOTHING
    # in _persist_event keeps the original Event row, so created_at is
    # stable. The outcome UPDATE writes the same values.
    await consumer.handle(message)
    with engine.connect() as conn:
        second = conn.execute(
            sa.text(
                "SELECT outcome_state, outcome_pr_number, outcome_merged_at "
                "FROM triage_findings WHERE finding_id = :id"
            ),
            {"id": finding.finding_id},
        ).one()
    assert second.outcome_state == "merged"
    assert second.outcome_pr_number == _PR_NUMBER
    assert second.outcome_merged_at == first_merged_at
