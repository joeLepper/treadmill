"""Integration tests for the dispatch-publish replay loop.

Drives ``ReplayLoop.tick()`` against a live Postgres so the JSONB payload
column, UUID FKs, and the NOT EXISTS subquery that finds unresolved
markers are exercised exactly as production. The poll cadence itself is
not under test — ``tick()`` is called directly so tests don't have to
wait the configured tick interval.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest tests/test_replay_loop.py
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
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

from treadmill_api.coordination.replay import ReplayLoop
from treadmill_api.events import (
    DispatchPublishFailed,
    DispatchPublishReplayed,
    TaskRegistered,
)

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason="set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL (a DEDICATED test database) to run; requires `treadmill-local up`",
)




@pytest.fixture(scope="module")
def database_url() -> str:
    return TEST_DB_URL


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


# ── Test doubles ──────────────────────────────────────────────────────────────


class _FakePublisher:
    """Records ``publish`` calls. Configurable to raise on the first N
    calls so we can exercise the retry path."""

    def __init__(self, *, fail_first_n: int = 0) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self._fail_first_n = fail_first_n

    async def publish(self, event: Any, typed_payload: Any) -> None:
        if len(self.calls) < self._fail_first_n:
            self.calls.append((event, typed_payload))
            raise RuntimeError("simulated SNS publish failure")
        self.calls.append((event, typed_payload))


# ── Seed helpers ──────────────────────────────────────────────────────────────


def _seed_original_event_sync(engine: Engine) -> tuple[uuid.UUID, dict[str, Any]]:
    """Insert a plausible original Event row that the marker references.

    Uses a ``task.registered`` event because it carries enough surrounding
    state (plan_id, task_id, repo) for the typed registry to validate the
    re-publish without faking too much.

    Returns the event id + the dict payload (so the test can assert the
    publisher saw the same payload).
    """
    plan_id = uuid.uuid4()
    task_id = uuid.uuid4()
    wv_id = uuid.uuid4()
    payload = TaskRegistered(
        repo="cli-test/repo",
        title="A task",
        workflow_version_id=wv_id,
        plan_id=plan_id,
    ).model_dump(mode="json")
    with engine.begin() as conn:
        # Plans + tasks satisfy the events FKs (ON DELETE SET NULL on
        # the events side means we *could* skip them, but reading rows
        # later via the ORM is cleaner with the FKs populated).
        conn.execute(sa.text(
            "INSERT INTO plans (id, repo) VALUES (:id, 'cli-test/repo')"
        ), {"id": plan_id})
        conn.execute(sa.text(
            "INSERT INTO workflows (id) VALUES ('wf-author') "
            "ON CONFLICT DO NOTHING"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_versions (id, workflow_id, version) "
            "VALUES (:id, 'wf-author', 1)"
        ), {"id": wv_id})
        conn.execute(sa.text(
            "INSERT INTO tasks (id, plan_id, repo, title, workflow_version_id) "
            "VALUES (:id, :p, 'cli-test/repo', 't', :wv)"
        ), {"id": task_id, "p": plan_id, "wv": wv_id})
        event_id = conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, plan_id, task_id, payload) "
            "VALUES ('task', 'registered', :p, :t, CAST(:pl AS jsonb)) "
            "RETURNING id"
        ), {"p": plan_id, "t": task_id, "pl": _to_json(payload)}).scalar()
    return event_id, payload


def _seed_marker_sync(
    engine: Engine,
    *,
    original_event_id: uuid.UUID,
    target: str = "sns",
) -> uuid.UUID:
    """Insert a ``dispatch_publish_failed`` Event row referencing
    *original_event_id*. Returns the marker's event id."""
    marker = DispatchPublishFailed(
        original_event_id=original_event_id,
        target=target,  # type: ignore[arg-type]
        error_message="simulated transport failure",
        attempted_at=datetime.now(timezone.utc),
    )
    payload_json = _to_json(marker.model_dump(mode="json"))
    with engine.begin() as conn:
        marker_id = conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, payload) "
            "VALUES ('_internal', 'dispatch_publish_failed', CAST(:pl AS jsonb)) "
            "RETURNING id"
        ), {"pl": payload_json}).scalar()
    return marker_id


def _seed_replayed_sibling_sync(
    engine: Engine,
    *,
    marker_id: uuid.UUID,
    original_event_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a ``dispatch_publish_replayed`` Event row pointing at
    *marker_id*. Used by the already-replayed-test setup."""
    sibling = DispatchPublishReplayed(
        original_failure_event_id=marker_id,
        original_event_id=original_event_id,
        replayed_at=datetime.now(timezone.utc),
    )
    payload_json = _to_json(sibling.model_dump(mode="json"))
    with engine.begin() as conn:
        sibling_id = conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, payload) "
            "VALUES ('_internal', 'dispatch_publish_replayed', "
            "CAST(:pl AS jsonb)) RETURNING id"
        ), {"pl": payload_json}).scalar()
    return sibling_id


def _to_json(value: Any) -> str:
    """Serialize a dict to a JSON string for the JSONB column."""
    import json
    return json.dumps(value)


def _count_replayed_for(engine: Engine, marker_id: uuid.UUID) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.text(
            "SELECT COUNT(*) FROM events "
            "WHERE entity_type='_internal' "
            "  AND action='dispatch_publish_replayed' "
            "  AND (payload->>'original_failure_event_id') = :mid"
        ), {"mid": str(marker_id)}).scalar() or 0


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_re_publishes_original_event_and_marks_replayed(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """One tick: a single unresolved marker gets its original event
    re-published AND a ``dispatch_publish_replayed`` sibling lands in
    the events table."""
    original_id, original_payload = _seed_original_event_sync(engine)
    marker_id = _seed_marker_sync(engine, original_event_id=original_id)

    publisher = _FakePublisher()
    loop = ReplayLoop(publisher=publisher, sessionmaker=session_factory)
    replayed = await loop.tick()

    assert replayed == 1
    assert len(publisher.calls) == 1
    # The publisher saw the original event (not the marker).
    sent_event, sent_payload = publisher.calls[0]
    assert sent_event.id == original_id
    assert sent_event.entity_type == "task"
    assert sent_event.action == "registered"
    # The typed payload reconstitutes from the JSONB column.
    assert isinstance(sent_payload, TaskRegistered)
    assert sent_payload.repo == original_payload["repo"]
    # And a resolution sibling exists for the marker.
    assert _count_replayed_for(engine, marker_id) == 1


@pytest.mark.asyncio
async def test_replay_skips_already_replayed_markers(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """A marker with an existing ``dispatch_publish_replayed`` sibling
    is treated as resolved. The publisher is NOT called again."""
    original_id, _ = _seed_original_event_sync(engine)
    marker_id = _seed_marker_sync(engine, original_event_id=original_id)
    # Pre-seed the resolution sibling so the marker is already healed.
    _seed_replayed_sibling_sync(
        engine, marker_id=marker_id, original_event_id=original_id
    )

    publisher = _FakePublisher()
    loop = ReplayLoop(publisher=publisher, sessionmaker=session_factory)
    replayed = await loop.tick()

    assert replayed == 0
    assert publisher.calls == []
    # And the one existing sibling stays the only one (no double-write).
    assert _count_replayed_for(engine, marker_id) == 1


@pytest.mark.asyncio
async def test_replay_retries_on_publish_failure(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Tick 1: publisher raises — no resolution sibling is written.
    Tick 2: publisher succeeds — the marker resolves on this tick."""
    original_id, _ = _seed_original_event_sync(engine)
    marker_id = _seed_marker_sync(engine, original_event_id=original_id)

    publisher = _FakePublisher(fail_first_n=1)
    loop = ReplayLoop(publisher=publisher, sessionmaker=session_factory)

    # Tick 1: publish raises; marker stays unresolved.
    replayed1 = await loop.tick()
    assert replayed1 == 0
    assert _count_replayed_for(engine, marker_id) == 0
    # The publisher *was* called even though it raised.
    assert len(publisher.calls) == 1

    # Tick 2: publisher succeeds — same marker, same original, now heals.
    replayed2 = await loop.tick()
    assert replayed2 == 1
    assert _count_replayed_for(engine, marker_id) == 1
    assert len(publisher.calls) == 2


@pytest.mark.asyncio
async def test_replay_skips_sqs_target_markers_for_now(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """v0 scope: SQS-target markers stay unresolved. The replay loop
    only knows how to re-issue SNS publishes through the events publisher;
    SQS work-queue claims need a different transport that this loop does
    not own. Future work adds that arm; today we leave the marker."""
    original_id, _ = _seed_original_event_sync(engine)
    marker_id = _seed_marker_sync(
        engine, original_event_id=original_id, target="sqs"
    )

    publisher = _FakePublisher()
    loop = ReplayLoop(publisher=publisher, sessionmaker=session_factory)
    replayed = await loop.tick()

    assert replayed == 0
    assert publisher.calls == []
    # Marker stays unresolved.
    assert _count_replayed_for(engine, marker_id) == 0
