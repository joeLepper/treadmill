"""Integration tests for the dispatcher's task_dependencies gate (D.2).

These tests exercise the gate against live Postgres so the
``task_dependencies`` rows, the ``plan_status`` VIEW, and the
``events``-table inserts all behave exactly as they will in production.

The gate's responsibility (per the 2026-05-11 closure plan D.2):

  * Evaluate every ``task_dependencies.expression`` row for the task
    against current DB state.
  * If any dependency is unsatisfied, persist the WorkflowRun + steps
    (so the task exists in the run graph) but SKIP the ``step.ready``
    event publish + SQS claim send. The consumer's re-evaluation pass
    (D.6) picks the task up later when the dep becomes satisfied.

The "no inline dispatch" rule matters: the dispatcher's job is to read
state, not to chain side-effects. Seeding a satisfying event mid-test
must not cause the original (unsatisfied) dispatch to retroactively
publish — only a fresh ``dispatch_task`` call dispatches.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest \
      services/api/tests/test_integration_dispatch_dependency_gate.py
"""

from __future__ import annotations

import json
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

from treadmill_api.dispatch import Dispatcher
from treadmill_api.models import Task

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
    "task_validations",
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
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def publish(self, event: object, payload: object) -> None:
        self.calls.append((event, payload))


class _RecordingSqs:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def send_message(self, **kwargs: object) -> None:
        self.sent.append(kwargs)


# ── Seeding helpers ───────────────────────────────────────────────────────────


def _seed_two_task_plan_with_dep(
    engine: Engine, *, activate_plan: bool = True,
) -> dict[str, uuid.UUID]:
    """Build the minimum graph: workflow + version + step + role + plan
    + two tasks (``t0`` and ``t1`` where t1 depends on ``task.t0.pr_merged``).
    Returns the ids for use by the assertions.

    When ``activate_plan`` is True the plan has a ``plan.activated`` event
    so it resolves to ``active`` in the VIEW — keeps the D.2 gate as the
    only deferral cause.
    """
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id) VALUES ('wf-author') ON CONFLICT DO NOTHING"
        ))
        wv_id = conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES ('wf-author', 1) RETURNING id"
        )).scalar()
        conn.execute(sa.text(
            "INSERT INTO roles (id, model, system_prompt, output_kind) "
            "VALUES ('role-author', 'claude', '', 'code') ON CONFLICT DO NOTHING"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_version_steps "
            "(workflow_version_id, step_index, step_name, role_id) "
            "VALUES (:wv, 0, 'author', 'role-author')"
        ), {"wv": wv_id})
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES ('test/dep-gate') RETURNING id"
        )).scalar()
        if activate_plan:
            conn.execute(sa.text(
                "INSERT INTO events (entity_type, action, plan_id, payload) "
                "VALUES ('plan', 'activated', :p, '{}'::jsonb)"
            ), {"p": plan_id})
        t0_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, 'test/dep-gate', 't0', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv_id}).scalar()
        t1_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, 'test/dep-gate', 't1', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv_id}).scalar()
        conn.execute(sa.text(
            "INSERT INTO task_dependencies (task_id, expression) "
            "VALUES (:t1, :expr)"
        ), {"t1": t1_id, "expr": f"task.{t0_id}.pr_merged"})
    return {"plan_id": plan_id, "wv_id": wv_id, "t0_id": t0_id, "t1_id": t1_id}


def _seed_single_task_plan(engine: Engine) -> dict[str, uuid.UUID]:
    """Single-task plan, no deps. Used for the regression-guard test
    asserting the dispatcher still publishes when no gates apply."""
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id) VALUES ('wf-author') ON CONFLICT DO NOTHING"
        ))
        wv_id = conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES ('wf-author', 1) RETURNING id"
        )).scalar()
        conn.execute(sa.text(
            "INSERT INTO roles (id, model, system_prompt, output_kind) "
            "VALUES ('role-author', 'claude', '', 'code') ON CONFLICT DO NOTHING"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_version_steps "
            "(workflow_version_id, step_index, step_name, role_id) "
            "VALUES (:wv, 0, 'author', 'role-author')"
        ), {"wv": wv_id})
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES ('test/dep-gate') RETURNING id"
        )).scalar()
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, plan_id, payload) "
            "VALUES ('plan', 'activated', :p, '{}'::jsonb)"
        ), {"p": plan_id})
        t_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, 'test/dep-gate', 't', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv_id}).scalar()
    return {"plan_id": plan_id, "wv_id": wv_id, "task_id": t_id}


async def _load_task(
    factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID,
) -> Task:
    async with factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        return task


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_task_plan_only_dispatches_unblocked(
    truncate: None,
    engine: Engine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """t0 has no deps → dispatches (step.ready event + SQS claim).
    t1 depends on ``task.t0.pr_merged`` (not yet merged) → run + steps
    persist but no step.ready event and no SQS claim."""
    ids = _seed_two_task_plan_with_dep(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    dispatcher = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )

    async with session_factory() as session:
        t0 = await session.get(Task, ids["t0_id"])
        await dispatcher.dispatch_task(session, t0)
        t1 = await session.get(Task, ids["t1_id"])
        await dispatcher.dispatch_task(session, t1)
        await session.commit()

    # t0 dispatched: step.ready event + SQS claim referencing t0.
    with engine.connect() as conn:
        t0_ready = conn.execute(sa.text(
            "SELECT count(*) FROM events "
            "WHERE task_id = :id AND entity_type = 'step' AND action = 'ready'"
        ), {"id": ids["t0_id"]}).scalar()
        t1_ready = conn.execute(sa.text(
            "SELECT count(*) FROM events "
            "WHERE task_id = :id AND entity_type = 'step' AND action = 'ready'"
        ), {"id": ids["t1_id"]}).scalar()
        # Both tasks have a WorkflowRun + step rows regardless of the gate.
        t1_runs = conn.execute(sa.text(
            "SELECT count(*) FROM workflow_runs WHERE task_id = :id"
        ), {"id": ids["t1_id"]}).scalar()
        t1_steps = conn.execute(sa.text(
            "SELECT count(*) FROM workflow_run_steps wrs "
            "JOIN workflow_runs wr ON wr.id = wrs.run_id "
            "WHERE wr.task_id = :id"
        ), {"id": ids["t1_id"]}).scalar()
    assert t0_ready == 1
    assert t1_ready == 0
    assert t1_runs == 1
    assert t1_steps == 1

    # Exactly one publish + one SQS send — both for t0.
    assert len(publisher.calls) == 1
    assert len(sqs.sent) == 1
    body = json.loads(sqs.sent[0]["MessageBody"])
    assert body["task_id"] == str(ids["t0_id"])


@pytest.mark.asyncio
async def test_seeding_pr_merged_event_doesnt_dispatch_inline(
    truncate: None,
    engine: Engine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed the satisfying ``github.pr_merged`` event, then call
    ``dispatch_task`` for t1 directly: t1 now dispatches. Two halves:

      * Confirms the gate evaluates against current DB state at call
        time (not snapshot-on-create).
      * Validates the D.6 hand-off contract: the consumer's re-evaluation
        pass simply re-calls ``dispatch_task`` on candidate tasks; the
        gate now passes and dispatch fires.
    """
    ids = _seed_two_task_plan_with_dep(engine)

    # Seed the satisfying event — t0's PR has merged.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, task_id, payload) "
            "VALUES ('github', 'pr_merged', :id, '{}'::jsonb)"
        ), {"id": ids["t0_id"]})

    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    dispatcher = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    async with session_factory() as session:
        t1 = await session.get(Task, ids["t1_id"])
        await dispatcher.dispatch_task(session, t1)
        await session.commit()

    # t1 dispatched: step.ready exists, claim went out.
    with engine.connect() as conn:
        ready_count = conn.execute(sa.text(
            "SELECT count(*) FROM events "
            "WHERE task_id = :id AND entity_type = 'step' AND action = 'ready'"
        ), {"id": ids["t1_id"]}).scalar()
    assert ready_count == 1
    assert len(publisher.calls) == 1
    assert len(sqs.sent) == 1
    body = json.loads(sqs.sent[0]["MessageBody"])
    assert body["task_id"] == str(ids["t1_id"])


@pytest.mark.asyncio
async def test_unblocked_task_dispatches_immediately(
    truncate: None,
    engine: Engine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression guard: a task with no dependencies (and an active plan)
    dispatches normally — gate stack is transparent for the happy path."""
    ids = _seed_single_task_plan(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    dispatcher = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    async with session_factory() as session:
        task = await session.get(Task, ids["task_id"])
        await dispatcher.dispatch_task(session, task)
        await session.commit()

    with engine.connect() as conn:
        ready_count = conn.execute(sa.text(
            "SELECT count(*) FROM events "
            "WHERE task_id = :id AND entity_type = 'step' AND action = 'ready'"
        ), {"id": ids["task_id"]}).scalar()
    assert ready_count == 1
    assert len(publisher.calls) == 1
    assert len(sqs.sent) == 1


@pytest.mark.asyncio
async def test_idempotent_redispatch_is_noop(
    truncate: None,
    engine: Engine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Hand-off contract with D.6's re-evaluation pass: calling
    ``dispatch_task`` twice for the same task must not double-publish or
    double-send. The second call returns the existing run_id."""
    ids = _seed_single_task_plan(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    dispatcher = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    async with session_factory() as session:
        task = await session.get(Task, ids["task_id"])
        first_run = await dispatcher.dispatch_task(session, task)
        await session.commit()
    async with session_factory() as session:
        task = await session.get(Task, ids["task_id"])
        second_run = await dispatcher.dispatch_task(session, task)
        await session.commit()

    assert first_run == second_run
    # Still only one publish + one send across the two calls.
    assert len(publisher.calls) == 1
    assert len(sqs.sent) == 1
    with engine.connect() as conn:
        ready_count = conn.execute(sa.text(
            "SELECT count(*) FROM events "
            "WHERE task_id = :id AND entity_type = 'step' AND action = 'ready'"
        ), {"id": ids["task_id"]}).scalar()
        run_count = conn.execute(sa.text(
            "SELECT count(*) FROM workflow_runs WHERE task_id = :id"
        ), {"id": ids["task_id"]}).scalar()
    assert ready_count == 1
    assert run_count == 1
