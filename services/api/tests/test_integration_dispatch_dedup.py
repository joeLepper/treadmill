"""Integration tests for the dispatch dedup table + trigger integration (ADR-0026).

Drives ``CoordinationConsumer.handle()`` against live Postgres to prove:

  * A first ``pr_synchronize`` event for (repo, pr, head_sha) creates a
    single ``wf-review`` run and inserts a ``workflow_dispatch_dedup``
    row.
  * A second delivery of the same event creates NO additional run; the
    dedup row's PK constraint catches the redundant insert and the
    helper logs INFO + skips.
  * A third event with the same (repo, pr) but a new ``head_sha`` does
    create a fresh run (the dedup_key is different).
  * The dedup row's ``workflow_run_id`` column is backfilled to the
    real run's id after the first dispatch lands.
  * Opt-out workflows (wf-author, wf-plan) and events missing
    discriminator fields (e.g. pr_review_submitted lacking review_id
    today) fall through to unconditional dispatch.

Gates
-----

  * ``TREADMILL_INTEGRATION=1`` — live Postgres available;
  * ``treadmill-local up`` is NOT required (we don't hit SQS/SNS).
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

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
from treadmill_api.dispatch import Dispatcher

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires Postgres",
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
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


_TEST_TABLES = (
    "events",
    "workflow_dispatch_dedup",
    "workflow_run_steps",
    "workflow_runs",
    "task_prs",
    "task_dependencies",
    "tasks",
    "plans",
    "workflow_version_steps",
    "workflow_versions",
    "workflows",
    "role_skills",
    "role_hooks",
    "skills",
    "hooks",
    "roles",
    "event_triggers",
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


# ── Test doubles ────────────────────────────────────────────────────────────


class _RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    async def publish(self, event: Any, payload: Any) -> None:
        self.calls.append((event, payload))


class _RecordingSqs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


# ── Seeding helpers (mirrors test_integration_event_triggers.py) ────────────


_WORKFLOW_DEFS: list[tuple[str, str]] = [
    ("wf-author", "role-code-author"),
    ("wf-review", "role-reviewer"),
    ("wf-validate", "role-validator"),
    ("wf-feedback", "role-feedback-analyzer"),
    ("wf-ci-fix", "role-ci-analyzer"),
    ("wf-conflict", "role-conflict-analyzer"),
    ("wf-plan", "role-planner"),
]


_TRIGGER_MAPPINGS: list[tuple[str, str]] = [
    ("pr_opened", "wf-review"),
    ("pr_synchronize", "wf-review"),
    ("pr_review_submitted", "wf-feedback"),
    ("check_run_completed", "wf-ci-fix"),
    ("pr_conflict", "wf-conflict"),
]


def _seed_world(
    engine: Engine,
    *,
    repo: str = "acme/myapp",
    pr_number: int = 42,
) -> uuid.UUID:
    """Seed catalog + a task with a task_prs bridge. Returns task_id."""
    wv_ids: dict[str, uuid.UUID] = {}
    with engine.begin() as conn:
        for role_id in {r for _, r in _WORKFLOW_DEFS}:
            conn.execute(sa.text(
                "INSERT INTO roles (id, model, system_prompt, output_kind) "
                "VALUES (:r, 'claude', '', 'code') ON CONFLICT DO NOTHING"
            ), {"r": role_id})
        for workflow_id, role_id in _WORKFLOW_DEFS:
            conn.execute(sa.text(
                "INSERT INTO workflows (id) VALUES (:w) ON CONFLICT DO NOTHING"
            ), {"w": workflow_id})
            wv_id = conn.execute(sa.text(
                "INSERT INTO workflow_versions (workflow_id, version) "
                "VALUES (:w, 1) RETURNING id"
            ), {"w": workflow_id}).scalar()
            wv_ids[workflow_id] = wv_id
            conn.execute(sa.text(
                "INSERT INTO workflow_version_steps "
                "(workflow_version_id, step_index, step_name, role_id) "
                "VALUES (:wv, 0, :name, :role)"
            ), {"wv": wv_id, "name": "step-0", "role": role_id})
        for event_type, workflow_id in _TRIGGER_MAPPINGS:
            conn.execute(sa.text(
                "INSERT INTO event_triggers "
                "(repo, event_type, workflow_id, version_strategy, enabled) "
                "VALUES (NULL, :et, :w, 'latest', TRUE) "
                "ON CONFLICT DO NOTHING"
            ), {"et": event_type, "w": workflow_id})
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES (:repo) RETURNING id"
        ), {"repo": repo}).scalar()
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, plan_id, payload) "
            "VALUES ('plan', 'activated', :p, '{}'::jsonb)"
        ), {"p": plan_id})
        task_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, :repo, 't', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv_ids["wf-author"], "repo": repo}).scalar()
        conn.execute(sa.text(
            "INSERT INTO task_prs (repo, pr_number, task_id, branch) "
            "VALUES (:repo, :pr, :t, 'task/foo')"
        ), {"repo": repo, "pr": pr_number, "t": task_id})
    return task_id


def _make_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    publisher: _RecordingPublisher,
    sqs: _RecordingSqs,
) -> CoordinationConsumer:
    dispatcher = Dispatcher(
        publisher=publisher,
        sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    return CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=session_factory,
        dispatcher=dispatcher,
        publisher=publisher,
    )


def _count_runs_for_workflow(
    engine: Engine, task_id: uuid.UUID, workflow_id: str,
) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.text(
            """
            SELECT COUNT(*) FROM workflow_runs wr
            JOIN workflow_versions wv ON wv.id = wr.workflow_version_id
            WHERE wr.task_id = :t AND wv.workflow_id = :w
            """
        ), {"t": task_id, "w": workflow_id}).scalar()


def _dedup_rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.text(
            "SELECT dedup_key, workflow_run_id FROM workflow_dispatch_dedup "
            "ORDER BY dedup_key"
        )).all())


def _sync_event_record(
    *, repo: str, pr_number: int, head_sha: str,
) -> dict[str, Any]:
    return {
        "entity_type": "github",
        "action": "pr_synchronize",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": repo, "pr_number": pr_number,
            "sender": "alice", "head_sha": head_sha,
        },
    }


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_pr_synchronize_creates_run_and_dedup_row(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """A first ``pr_synchronize`` event creates a single ``wf-review``
    run and a single dedup row keyed on the (repo, pr, head_sha)
    triple. The dedup row's ``workflow_run_id`` is backfilled to the
    run's id (no sentinel left behind)."""
    repo, pr_number = "acme/myapp", 42
    head_sha = "a" * 40
    task_id = _seed_world(engine, repo=repo, pr_number=pr_number)

    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_sync_event_record(
        repo=repo, pr_number=pr_number, head_sha=head_sha,
    ))

    assert _count_runs_for_workflow(engine, task_id, "wf-review") == 1
    rows = _dedup_rows(engine)
    review_rows = [r for r in rows if r.dedup_key.startswith("wf-review:")]
    assert len(review_rows) == 1
    assert review_rows[0].dedup_key == (
        f"wf-review:{repo}:pr={pr_number},sha={head_sha}"
    )
    # The workflow_run_id was backfilled (not the sentinel).
    assert review_rows[0].workflow_run_id != uuid.UUID(
        "00000000-0000-0000-0000-000000000000"
    )


@pytest.mark.asyncio
async def test_redelivery_of_same_event_is_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Two identical ``pr_synchronize`` events fire the consumer, but
    only one ``wf-review`` run lands — the second insertion hits the
    dedup PK constraint and the helper skips dispatch."""
    repo, pr_number = "acme/myapp", 42
    head_sha = "b" * 40
    task_id = _seed_world(engine, repo=repo, pr_number=pr_number)

    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    record = _sync_event_record(
        repo=repo, pr_number=pr_number, head_sha=head_sha,
    )

    # First delivery — creates a run + dedup row.
    await consumer.handle(record)
    # Second delivery — fresh event_id (the audit-row dedup is per-id;
    # the dispatch dedup is per-payload-discriminator). Should be
    # skipped at the trigger evaluator's pre-dispatch gate.
    record2 = _sync_event_record(
        repo=repo, pr_number=pr_number, head_sha=head_sha,
    )
    await consumer.handle(record2)

    assert _count_runs_for_workflow(engine, task_id, "wf-review") == 1
    # Still exactly one wf-review dedup row.
    review_rows = [
        r for r in _dedup_rows(engine)
        if r.dedup_key.startswith("wf-review:")
    ]
    assert len(review_rows) == 1


@pytest.mark.asyncio
async def test_new_head_sha_creates_a_second_run(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """A second ``pr_synchronize`` against the same PR but a NEW
    ``head_sha`` produces a fresh wf-review run — the dedup_key differs
    on the SHA so the PK constraint doesn't fire."""
    repo, pr_number = "acme/myapp", 42
    task_id = _seed_world(engine, repo=repo, pr_number=pr_number)

    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_sync_event_record(
        repo=repo, pr_number=pr_number, head_sha="a" * 40,
    ))
    await consumer.handle(_sync_event_record(
        repo=repo, pr_number=pr_number, head_sha="b" * 40,
    ))

    assert _count_runs_for_workflow(engine, task_id, "wf-review") == 2
    review_rows = [
        r for r in _dedup_rows(engine)
        if r.dedup_key.startswith("wf-review:")
    ]
    assert len(review_rows) == 2


@pytest.mark.asyncio
async def test_pr_review_submitted_dispatches_without_dedup_when_review_id_absent(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """``pr_review_submitted`` payloads do not currently carry
    ``review_id`` (normalizer follow-up). The wf-feedback builder
    returns None → the helper falls through to unconditional dispatch.

    Two deliveries of the same payload therefore produce TWO
    wf-feedback runs (no dedup yet). Once the normalizer is extended
    to emit ``review_id``, this test will need updating — flagged in
    the report.
    """
    repo, pr_number = "acme/myapp", 42
    task_id = _seed_world(engine, repo=repo, pr_number=pr_number)

    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    def _record() -> dict[str, Any]:
        return {
            "entity_type": "github",
            "action": "pr_review_submitted",
            "event_id": str(uuid.uuid4()),
            "payload": {
                "repo": repo, "pr_number": pr_number,
                "sender": "alice", "state": "changes_requested",
            },
        }

    await consumer.handle(_record())
    await consumer.handle(_record())

    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 2
    # No dedup rows for wf-feedback (None key → no insert).
    feedback_rows = [
        r for r in _dedup_rows(engine)
        if r.dedup_key.startswith("wf-feedback:")
    ]
    assert len(feedback_rows) == 0
