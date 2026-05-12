"""Integration tests for the coordination consumer's re-evaluation pass.

Per the 2026-05-11 closure plan D.6, when a ``step.completed`` or
``plan.activated`` event arrives the consumer runs a re-evaluation pass:
every task whose ``task_status.derived_status = 'registered'`` AND
parent ``plan_status.derived_status = 'active'`` is dispatched (the
dispatcher gates further on ``task_dependencies`` + ``plan_status``).

These tests cover three load-bearing properties:

  * Two-task dependency chain — ``t0`` completes, ``github.pr_merged``
    arrives, t1 dispatches.
  * Intent-only plan + ``plan.activated`` — the single task on the plan
    dispatches.
  * Idempotency — the same trigger event delivered twice produces at
    most one new ``step.ready`` event for the downstream task.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest tests/test_integration_redispatch.py
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

from treadmill_api.coordination.consumer import CoordinationConsumer
from treadmill_api.dispatch import Dispatcher
from treadmill_api.eventbus import LoggingEventPublisher

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
    "roles",
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


# ── Test fixtures ─────────────────────────────────────────────────────────────


def _seed_workflow_and_role(engine: Engine) -> uuid.UUID:
    """Insert ``wf-author`` workflow with one step and a role; return the
    workflow_version id."""
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id) VALUES ('wf-author') "
            "ON CONFLICT DO NOTHING"
        ))
        wv_id = conn.execute(sa.text(
            "SELECT id FROM workflow_versions WHERE workflow_id = 'wf-author' "
            "AND version = 1"
        )).scalar()
        if wv_id is None:
            wv_id = conn.execute(sa.text(
                "INSERT INTO workflow_versions (workflow_id, version) "
                "VALUES ('wf-author', 1) RETURNING id"
            )).scalar()
        conn.execute(sa.text(
            "INSERT INTO roles (id, model, system_prompt, output_kind) "
            "VALUES ('role-author', 'claude-opus-4-7', 'be a coder', 'code') "
            "ON CONFLICT DO NOTHING"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_version_steps "
            "(workflow_version_id, step_index, step_name, role_id) "
            "VALUES (:wv, 0, 'author', 'role-author') "
            "ON CONFLICT DO NOTHING"
        ), {"wv": wv_id})
    return wv_id


def _make_plan(engine: Engine, repo: str, *, active: bool = True) -> uuid.UUID:
    """Insert a plan and (if ``active``) the lifecycle events that the
    ``plan_status`` VIEW reads to resolve the plan to ``active``."""
    with engine.begin() as conn:
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES (:r) RETURNING id"
        ), {"r": repo}).scalar()
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, plan_id, payload) "
            "VALUES ('plan', 'registered', :p, CAST(:pay AS jsonb))"
        ), {"p": plan_id, "pay": json.dumps({"repo": repo})})
        if active:
            conn.execute(sa.text(
                "INSERT INTO events (entity_type, action, plan_id, payload, created_at) "
                "VALUES ('plan', 'activated', :p, CAST('{}' AS jsonb), "
                "now() + interval '1 microsecond')"
            ), {"p": plan_id})
    return plan_id


def _make_task(
    engine: Engine,
    plan_id: uuid.UUID,
    repo: str,
    title: str,
    wv_id: uuid.UUID,
) -> uuid.UUID:
    with engine.begin() as conn:
        return conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, :r, :t, :w) RETURNING id"
        ), {"p": plan_id, "r": repo, "t": title, "w": wv_id}).scalar()


def _add_dependency(engine: Engine, task_id: uuid.UUID, expression: str) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO task_dependencies (task_id, expression) "
            "VALUES (:t, :e)"
        ), {"t": task_id, "e": expression})


def _make_run_with_completed_step(
    engine: Engine, task_id: uuid.UUID, wv_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Dispatch a run + completed step for a task synchronously, the way
    the dispatcher would. Returns (run_id, step_id)."""
    with engine.begin() as conn:
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs (task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'registered') RETURNING id"
        ), {"t": task_id, "wv": wv_id}).scalar()
        step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status, completed_at) "
            "VALUES (:r, 0, 'author', 'role-author', 'completed', now()) "
            "RETURNING id"
        ), {"r": run_id}).scalar()
    return run_id, step_id


class _FakeSqs:
    """Records SQS send_message calls so we can assert the work-queue
    side of the dispatcher fired."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


def _build_consumer(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[CoordinationConsumer, _FakeSqs]:
    """Build a consumer wired with a real Dispatcher → fake SQS so we
    can drive the re-evaluation pass against live DB state."""
    sqs = _FakeSqs()
    dispatcher = Dispatcher(
        publisher=LoggingEventPublisher(),
        sqs_client=sqs,
        work_queue_url="https://sqs.example/work",
    )
    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=session_factory,
        dispatcher=dispatcher,
    )
    return consumer, sqs


# ── Two-task chain (t0 → t1) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redispatch_dispatches_dependent_task_when_chain_unblocks(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """t0 dispatches and runs; we synthesize its run-completed state and
    its ``github.pr_merged`` event so t1's dependency expression is now
    satisfied. Driving a ``step.completed`` event triggers the
    re-evaluation pass, which finds t1 with ``task_status.derived_status
    = 'registered'`` and dispatches it."""
    wv_id = _seed_workflow_and_role(engine)
    plan_id = _make_plan(engine, "chain/repo", active=True)

    # t0: completed run + PR merged.
    t0 = _make_task(engine, plan_id, "chain/repo", "t0", wv_id)
    run_id, step_id = _make_run_with_completed_step(engine, t0, wv_id)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, task_id, payload) "
            "VALUES ('github', 'pr_merged', :t, CAST(:pay AS jsonb))"
        ), {"t": t0, "pay": json.dumps({
            "repo": "chain/repo", "pr_number": 1, "sender": "alice",
        })})

    # t1: depends on t0.pr_merged.
    t1 = _make_task(engine, plan_id, "chain/repo", "t1", wv_id)
    _add_dependency(engine, t1, f"task.{t0}.pr_merged")

    # Sanity: confirm t1 is currently 'registered' (deps satisfied, no
    # run yet) — the precondition for the re-eval pass to fire.
    with engine.connect() as conn:
        status = conn.execute(
            sa.text("SELECT derived_status FROM task_status WHERE id = :id"),
            {"id": t1},
        ).scalar()
    assert status == "registered"

    consumer, sqs = _build_consumer(session_factory)

    # Drive any step.completed event — what matters is that ``handle()``
    # fires the re-evaluation pass. We use t0's step.
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "authored task/t0",
                "decision": "pushed",
                "artifacts": [{"kind": "branch", "value": "task/t0"}],
            },
        },
    })

    # t1 should have a workflow_run row now (the dispatcher created it).
    with engine.connect() as conn:
        t1_runs = conn.execute(sa.text(
            "SELECT COUNT(*) FROM workflow_runs WHERE task_id = :t"
        ), {"t": t1}).scalar()
    assert t1_runs == 1

    # The SQS work queue saw the claim for t1's first step.
    assert len(sqs.sent) >= 1
    bodies = [json.loads(m["MessageBody"]) for m in sqs.sent]
    assert any(b["task_id"] == str(t1) for b in bodies)


# ── Intent-only plan + plan.activated ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_activated_dispatches_pending_task(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """A plan registered without ``activated`` has tasks that are
    ``registered`` but the plan_status is still ``drafting`` — the
    re-eval pass filters them out. Once ``plan.activated`` arrives the
    pass dispatches the task."""
    wv_id = _seed_workflow_and_role(engine)
    plan_id = _make_plan(engine, "intent/repo", active=False)  # drafting
    task_id = _make_task(engine, plan_id, "intent/repo", "lone", wv_id)

    # Sanity: plan is drafting, task is registered.
    with engine.connect() as conn:
        ps = conn.execute(
            sa.text("SELECT derived_status FROM plan_status WHERE id = :id"),
            {"id": plan_id},
        ).scalar()
        ts = conn.execute(
            sa.text("SELECT derived_status FROM task_status WHERE id = :id"),
            {"id": task_id},
        ).scalar()
    assert ps == "drafting"
    assert ts == "registered"

    consumer, sqs = _build_consumer(session_factory)

    # Now insert the plan.activated event the same way the API would
    # (this is the event the consumer would see on the bus).
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, plan_id, payload) "
            "VALUES ('plan', 'activated', :p, CAST('{}' AS jsonb))"
        ), {"p": plan_id})

    await consumer.handle({
        "entity_type": "plan",
        "action": "activated",
        "plan_id": str(plan_id),
        "payload": {},
    })

    # The task dispatched — workflow_runs row exists.
    with engine.connect() as conn:
        runs = conn.execute(sa.text(
            "SELECT COUNT(*) FROM workflow_runs WHERE task_id = :t"
        ), {"t": task_id}).scalar()
    assert runs == 1
    assert len(sqs.sent) >= 1


# ── Idempotency ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redispatch_is_idempotent_under_duplicate_triggers(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Re-delivering a trigger event must not dispatch the same task
    twice. After the first delivery the task has a workflow_runs row, so
    ``task_status.derived_status`` is no longer ``'registered'`` and the
    pass filters it out — exactly one new ``step.ready`` event ever
    fires for the downstream task."""
    wv_id = _seed_workflow_and_role(engine)
    plan_id = _make_plan(engine, "idem/repo", active=True)
    t0 = _make_task(engine, plan_id, "idem/repo", "t0", wv_id)
    run_id, step_id = _make_run_with_completed_step(engine, t0, wv_id)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, task_id, payload) "
            "VALUES ('github', 'pr_merged', :t, CAST(:pay AS jsonb))"
        ), {"t": t0, "pay": json.dumps({
            "repo": "idem/repo", "pr_number": 1, "sender": "alice",
        })})

    t1 = _make_task(engine, plan_id, "idem/repo", "t1", wv_id)
    _add_dependency(engine, t1, f"task.{t0}.pr_merged")

    consumer, sqs = _build_consumer(session_factory)

    event = {
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "authored task/t0",
                "decision": "pushed",
                "artifacts": [{"kind": "branch", "value": "task/t0"}],
            },
        },
    }
    await consumer.handle(event)
    await consumer.handle(event)

    # Exactly one workflow_run for t1, exactly one step.ready event in
    # the audit log for the downstream task.
    with engine.connect() as conn:
        t1_runs = conn.execute(sa.text(
            "SELECT COUNT(*) FROM workflow_runs WHERE task_id = :t"
        ), {"t": t1}).scalar()
        t1_ready_events = conn.execute(sa.text(
            "SELECT COUNT(*) FROM events "
            "WHERE entity_type = 'step' AND action = 'ready' "
            "AND task_id = :t"
        ), {"t": t1}).scalar()
    assert t1_runs == 1
    assert t1_ready_events == 1
