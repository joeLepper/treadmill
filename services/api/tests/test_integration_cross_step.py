"""Integration tests for cross-step dispatch (Week-3 B.2).

Per ADR-0015 §"Cross-step dispatch", the coordination consumer takes
over after step 1 of a multi-step run completes: it publishes
``step.ready`` for the next step + sends an SQS work-queue claim. The
dispatcher (``dispatch.py``) stays responsible for the first-step
firing.

These tests drive ``CoordinationConsumer.handle()`` against a live
Postgres so the SQL idempotency guards (the ``LEFT JOIN events`` filter
in ``cross_step._find_next_pending_step``) are exercised exactly as
production.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest tests/test_integration_cross_step.py
"""

from __future__ import annotations

import json
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


class _RecordingPublisher:
    def __init__(self, raise_on_call: int | None = None) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self._raise_on_call = raise_on_call

    async def publish(self, event: Any, payload: Any) -> None:
        idx = len(self.calls)
        self.calls.append((event, payload))
        if self._raise_on_call is not None and idx == self._raise_on_call:
            raise RuntimeError("publish-failure-test")


class _RecordingSqs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


# ── Seeding helpers ───────────────────────────────────────────────────────────


class _MultiStepSeed:
    def __init__(
        self,
        *,
        plan_id: uuid.UUID,
        task_id: uuid.UUID,
        run_id: uuid.UUID,
        wv_id: uuid.UUID,
        step_ids: list[uuid.UUID],
        repo: str,
        workflow_slug: str,
    ) -> None:
        self.plan_id = plan_id
        self.task_id = task_id
        self.run_id = run_id
        self.wv_id = wv_id
        self.step_ids = step_ids
        self.repo = repo
        self.workflow_slug = workflow_slug


def _seed_multistep_run(
    engine: Engine,
    *,
    n_steps: int = 2,
    first_step_status: str = "running",
    repo: str = "cross-step/repo",
    workflow_slug: str = "wf-ci-fix",
) -> _MultiStepSeed:
    """Seed a run with N steps pre-created as ``pending`` (except step 0
    which defaults to ``running`` so step.completed can transition it).

    Mirrors the dispatcher's run-creation pattern: every step row
    materialised up-front. The cross-step dispatch path is responsible
    for ``step.ready`` events on indices 1..N-1.
    """
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id) VALUES (:wf) ON CONFLICT DO NOTHING"
        ), {"wf": workflow_slug})
        wv_id = conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES (:wf, 1) RETURNING id"
        ), {"wf": workflow_slug}).scalar()
        # Two distinct roles so step 1 ≠ step 0 (the multi-step shape
        # from ADR-0015's matrix uses analyzer → action).
        conn.execute(sa.text(
            "INSERT INTO roles (id, model, system_prompt) "
            "VALUES ('role-analyzer', 'claude', '') "
            "ON CONFLICT DO NOTHING"
        ))
        conn.execute(sa.text(
            "INSERT INTO roles (id, model, system_prompt) "
            "VALUES ('role-code-author', 'claude', '') "
            "ON CONFLICT DO NOTHING"
        ))
        for i in range(n_steps):
            role_id = "role-analyzer" if i == 0 else "role-code-author"
            step_name = f"step-{i}"
            conn.execute(sa.text(
                "INSERT INTO workflow_version_steps "
                "(workflow_version_id, step_index, step_name, role_id) "
                "VALUES (:wv, :idx, :name, :role)"
            ), {"wv": wv_id, "idx": i, "name": step_name, "role": role_id})

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
        ), {"p": plan_id, "wv": wv_id, "repo": repo}).scalar()
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs (task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'registered') RETURNING id"
        ), {"t": task_id, "wv": wv_id}).scalar()

        step_ids: list[uuid.UUID] = []
        for i in range(n_steps):
            role_id = "role-analyzer" if i == 0 else "role-code-author"
            status = first_step_status if i == 0 else "pending"
            sid = conn.execute(sa.text(
                "INSERT INTO workflow_run_steps "
                "(run_id, step_index, step_name, role_id, status) "
                "VALUES (:r, :idx, :name, :role, :st) RETURNING id"
            ), {
                "r": run_id, "idx": i, "name": f"step-{i}",
                "role": role_id, "st": status,
            }).scalar()
            step_ids.append(sid)

    return _MultiStepSeed(
        plan_id=plan_id, task_id=task_id, run_id=run_id, wv_id=wv_id,
        step_ids=step_ids, repo=repo, workflow_slug=workflow_slug,
    )


def _step_completed_event(
    *,
    step_id: uuid.UUID,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    commit_sha: str | None = None,
    decision: str = "plan-ready",
    payload_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "summary": "step ok",
        "decision": decision,
        "artifacts": [],
        "payload": payload_extras or {},
    }
    if commit_sha is not None:
        output["commit_sha"] = commit_sha
    return {
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "task_id": str(task_id),
        "run_id": str(run_id),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-11T10:00:00+00:00",
            "output": output,
        },
    }


def _step_failed_event(
    *,
    step_id: uuid.UUID,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    error: str = "analyzer blocked",
) -> dict[str, Any]:
    return {
        "entity_type": "step",
        "action": "failed",
        "step_id": str(step_id),
        "task_id": str(task_id),
        "run_id": str(run_id),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "failed_at": "2026-05-11T10:00:00+00:00",
            "error": error,
        },
    }


def _make_consumer_with_dispatcher(
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
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_completion_dispatches_next_step(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """B.2 happy path. A 2-step workflow; step 1 completes; cross-step
    dispatch publishes ``step.ready`` for step 2 + sends the SQS claim.
    The Event row is keyed on step 2's id; the SQS body contains all
    four IDs."""
    seed = _seed_multistep_run(engine, n_steps=2)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer_with_dispatcher(session_factory, publisher, sqs)

    await consumer.handle(_step_completed_event(
        step_id=seed.step_ids[0],
        task_id=seed.task_id,
        run_id=seed.run_id,
    ))

    # An Event row of (entity_type=step, action=ready, step_id=step_2) exists.
    with engine.connect() as conn:
        ready_rows = conn.execute(sa.text(
            "SELECT id, step_id FROM events "
            "WHERE entity_type = 'step' AND action = 'ready' "
            "AND step_id = :sid"
        ), {"sid": seed.step_ids[1]}).all()
    assert len(ready_rows) == 1

    # Publisher was called with the step.ready payload.
    publish_actions = [e.action for e, _ in publisher.calls]
    assert publish_actions == ["ready"]

    # SQS claim was sent; body identifies step 2.
    assert len(sqs.sent) == 1
    body = json.loads(sqs.sent[0]["MessageBody"])
    assert body["step_id"] == str(seed.step_ids[1])
    assert body["task_id"] == str(seed.task_id)
    assert body["run_id"] == str(seed.run_id)
    assert body["plan_id"] == str(seed.plan_id)


@pytest.mark.asyncio
async def test_step_completion_does_not_dispatch_when_last_step(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Single-step workflows (e.g. wf-author) have no next step; the
    cross-step path short-circuits silently. No additional step.ready
    Event row is written and no SQS claim is sent."""
    seed = _seed_multistep_run(engine, n_steps=1)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer_with_dispatcher(session_factory, publisher, sqs)

    await consumer.handle(_step_completed_event(
        step_id=seed.step_ids[0],
        task_id=seed.task_id,
        run_id=seed.run_id,
    ))

    with engine.connect() as conn:
        count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM events "
            "WHERE entity_type = 'step' AND action = 'ready'"
        )).scalar()
    assert count == 0
    assert publisher.calls == []
    assert sqs.sent == []


@pytest.mark.asyncio
async def test_step_failure_still_dispatches_next_step(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Per ADR-0015 §"No cancellation; no step skipping", a failed
    analyzer step still triggers the next (action) step. The action
    role sees a missing/blocked prior decision and emits its own no-op."""
    seed = _seed_multistep_run(engine, n_steps=2)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer_with_dispatcher(session_factory, publisher, sqs)

    await consumer.handle(_step_failed_event(
        step_id=seed.step_ids[0],
        task_id=seed.task_id,
        run_id=seed.run_id,
    ))

    # Step 2's step.ready event landed despite step 1 failing.
    with engine.connect() as conn:
        ready_rows = conn.execute(sa.text(
            "SELECT id FROM events "
            "WHERE entity_type = 'step' AND action = 'ready' "
            "AND step_id = :sid"
        ), {"sid": seed.step_ids[1]}).all()
    assert len(ready_rows) == 1
    assert len(sqs.sent) == 1


@pytest.mark.asyncio
async def test_cross_step_dispatch_is_idempotent_on_redelivery(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Re-delivering ``step.completed`` for step 1 must not dispatch
    step 2 twice. The LEFT JOIN events guard in
    ``cross_step._find_next_pending_step`` filters out the next step
    once a step.ready event exists for it."""
    seed = _seed_multistep_run(engine, n_steps=2)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer_with_dispatcher(session_factory, publisher, sqs)

    event = _step_completed_event(
        step_id=seed.step_ids[0],
        task_id=seed.task_id,
        run_id=seed.run_id,
    )
    # Second delivery — same record (event_id and all). We rebuild to
    # ensure the consumer's own audit-log dedupe doesn't mask the
    # cross-step idempotency we're testing for here.
    event_2 = _step_completed_event(
        step_id=seed.step_ids[0],
        task_id=seed.task_id,
        run_id=seed.run_id,
    )

    await consumer.handle(event)
    await consumer.handle(event_2)

    with engine.connect() as conn:
        count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM events "
            "WHERE entity_type = 'step' AND action = 'ready' "
            "AND step_id = :sid"
        ), {"sid": seed.step_ids[1]}).scalar()
    assert count == 1
    # SQS was sent exactly once.
    assert len(sqs.sent) == 1


@pytest.mark.asyncio
async def test_cross_step_dispatch_propagates_commit_sha(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Cross-step #5 — the prior step's envelope ``commit_sha`` must
    appear on the next step's ``step.ready`` Event row (so ADR-0013's
    mergeability VIEW can LATERAL-join it) AND in the SQS claim body
    (so the worker has the HEAD anchor in-band)."""
    seed = _seed_multistep_run(engine, n_steps=2)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer_with_dispatcher(session_factory, publisher, sqs)

    sha = "a" * 40
    await consumer.handle(_step_completed_event(
        step_id=seed.step_ids[0],
        task_id=seed.task_id,
        run_id=seed.run_id,
        commit_sha=sha,
    ))

    with engine.connect() as conn:
        ready_row = conn.execute(sa.text(
            "SELECT commit_sha FROM events "
            "WHERE entity_type = 'step' AND action = 'ready' "
            "AND step_id = :sid"
        ), {"sid": seed.step_ids[1]}).one()
    assert ready_row.commit_sha == sha

    assert len(sqs.sent) == 1
    body = json.loads(sqs.sent[0]["MessageBody"])
    assert body["commit_sha"] == sha


@pytest.mark.asyncio
async def test_cross_step_dispatch_failure_records_marker(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """When the SNS publisher raises on the cross-step ``step.ready``,
    the cross_step path persists a ``dispatch_publish_failed`` marker
    (Phase-3 closure A.8) referencing the original Event row. The
    replay loop reads those markers and re-publishes — that path is
    covered by ``test_replay_loop.py``."""
    seed = _seed_multistep_run(engine, n_steps=2)
    # Raise on the first publish (cross-step's step.ready).
    publisher = _RecordingPublisher(raise_on_call=0)
    sqs = _RecordingSqs()
    consumer = _make_consumer_with_dispatcher(session_factory, publisher, sqs)

    await consumer.handle(_step_completed_event(
        step_id=seed.step_ids[0],
        task_id=seed.task_id,
        run_id=seed.run_id,
    ))

    with engine.connect() as conn:
        markers = conn.execute(sa.text(
            "SELECT id, payload FROM events "
            "WHERE entity_type = '_internal' "
            "AND action = 'dispatch_publish_failed'"
        )).all()
        ready_rows = conn.execute(sa.text(
            "SELECT id FROM events "
            "WHERE entity_type = 'step' AND action = 'ready' "
            "AND step_id = :sid"
        ), {"sid": seed.step_ids[1]}).all()

    # The Event row landed (durable; replay heals) and a marker exists
    # referencing it.
    assert len(ready_rows) == 1
    assert len(markers) == 1
    marker_payload = markers[0].payload
    assert marker_payload["target"] == "sns"
    assert marker_payload["original_event_id"] == str(ready_rows[0].id)


# ── Regression: single-step (wf-author) still doesn't dispatch a next ─────────


@pytest.mark.asyncio
async def test_single_step_wf_author_run_dispatches_no_next_step(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Regression for existing wf-author single-step runs: completing
    the only step in the run must NOT trigger any spurious step.ready
    Event rows. Identical guarantee as the "last step" test, but
    framed against the original wf-author shape so any future regression
    in the helper's "next pending" SELECT shows here."""
    seed = _seed_multistep_run(engine, n_steps=1, workflow_slug="wf-author")
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer_with_dispatcher(session_factory, publisher, sqs)

    # wf-author's typical envelope shape — pushed + branch + pr_url.
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(seed.step_ids[0]),
        "task_id": str(seed.task_id),
        "run_id": str(seed.run_id),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-11T10:00:00+00:00",
            "output": {
                "summary": "branch pushed",
                "decision": "pushed",
                "commit_sha": "b" * 40,
                "artifacts": [
                    {"kind": "branch", "value": "task/regression"},
                ],
                "payload": {"pr_number": 1},
            },
        },
    })

    # No step.ready emerged from cross-step dispatch.
    with engine.connect() as conn:
        count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM events "
            "WHERE entity_type = 'step' AND action = 'ready'"
        )).scalar()
    assert count == 0
    assert publisher.calls == []
    assert sqs.sent == []
