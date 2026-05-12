"""Integration tests for the dispatcher's plan-active gate (D.5).

The gate (per the 2026-05-11 closure plan D.5):

  * Before evaluating dependencies, read ``plan_status.derived_status``
    for the task's plan. If it isn't ``active``, persist the WorkflowRun
    + step rows (so the task appears in the run graph) but skip the
    ``step.ready`` event publish + SQS claim send.

The critical edge case the gate must handle is Scenario 1 plan creation:
the plans router emits ``PlanRegistered`` + ``PlanActivated`` in the same
transaction as task creation (A.6). The dispatcher must see the
``plan.activated`` Event row when it reads the ``plan_status`` VIEW —
Postgres' read-your-own-writes semantics make that work, and these tests
prove it does.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest \
      services/api/tests/test_integration_plan_active_gate.py
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
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
DEFAULT_API_URL = "http://localhost:8088"


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def async_database_url(database_url: str) -> str:
    return database_url.replace("+psycopg", "+asyncpg")


@pytest.fixture(scope="module")
def api_url() -> str:
    return os.environ.get("TREADMILL_API_URL", DEFAULT_API_URL)


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


@pytest.fixture(scope="module")
def wait_for_api(api_url: str) -> None:
    """Block until the API container is reachable (Scenario 1 test uses it)."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"API not reachable at {api_url}")


# ── Seeding helpers ───────────────────────────────────────────────────────────


def _seed_wf_author(engine: Engine) -> uuid.UUID:
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
    return wv_id


def _seed_drafting_plan_with_task(
    engine: Engine, wv_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Plan with no ``plan.activated`` event (stays in drafting) and one
    task. Used by the Scenario-2 + manual-activate tests."""
    with engine.begin() as conn:
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES ('test/plan-gate') RETURNING id"
        )).scalar()
        task_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, 'test/plan-gate', 't', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv_id}).scalar()
    return {"plan_id": plan_id, "task_id": task_id}


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


_PLAN_DOC = """## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "Solo task"
    workflow: wf-author
    intent: solo
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: tests pass
```
"""


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_scenario_1_doc_submission_dispatches_in_same_txn(
    truncate: None,
    engine: Engine,
    api_url: str,
    wait_for_api: None,
) -> None:
    """Scenario 1: ``POST /plans`` with ``doc_content``. The plans router
    emits ``PlanRegistered`` + ``PlanActivated`` and then calls the
    dispatcher — all inside one transaction. The dispatcher's read of
    ``plan_status.derived_status`` must see ``active`` (the just-inserted
    Event row is visible to the same transaction in Postgres) and
    therefore must publish a ``step.ready`` event.

    Asserts on the DB after commit: a single ``step.ready`` event is the
    proof that the gate let the task through inside the same txn."""
    _seed_wf_author(engine)
    response = httpx.post(
        f"{api_url}/api/v1/plans",
        json={
            "repo": "test/plan-gate",
            "doc_path": "docs/plans/x.md",
            "doc_content": _PLAN_DOC,
        },
        timeout=10.0,
    )
    assert response.status_code == 201, response.text
    plan_id = response.json()["id"]

    with engine.connect() as conn:
        ready_count = conn.execute(sa.text(
            "SELECT count(*) FROM events e "
            "JOIN tasks t ON t.id = e.task_id "
            "WHERE t.plan_id = :p AND e.entity_type = 'step' AND e.action = 'ready'"
        ), {"p": plan_id}).scalar()
        plan_actions = [
            r.action for r in conn.execute(sa.text(
                "SELECT action FROM events "
                "WHERE plan_id = :p AND entity_type = 'plan' "
                "ORDER BY created_at"
            ), {"p": plan_id}).all()
        ]
    assert plan_actions == ["registered", "activated"]
    assert ready_count == 1  # exactly one step.ready — task dispatched


@pytest.mark.asyncio
async def test_scenario_2_intent_only_does_not_dispatch_task(
    truncate: None,
    engine: Engine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 2: a drafting plan (intent only — no ``plan.activated``
    event). Dispatching a task that belongs to it persists the run +
    steps but emits no ``step.ready`` and sends no SQS claim. The
    consumer's re-evaluation pass will dispatch when activation arrives.
    """
    wv_id = _seed_wf_author(engine)
    ids = _seed_drafting_plan_with_task(engine, wv_id)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    dispatcher = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    async with session_factory() as session:
        task = await session.get(Task, ids["task_id"])
        run_id = await dispatcher.dispatch_task(session, task)
        await session.commit()
    assert run_id is not None

    # Run + steps persisted (so re-evaluation can find the task) but no
    # step.ready event and no external sends.
    with engine.connect() as conn:
        ready_count = conn.execute(sa.text(
            "SELECT count(*) FROM events "
            "WHERE task_id = :id AND entity_type = 'step' AND action = 'ready'"
        ), {"id": ids["task_id"]}).scalar()
        run_count = conn.execute(sa.text(
            "SELECT count(*) FROM workflow_runs WHERE task_id = :id"
        ), {"id": ids["task_id"]}).scalar()
        step_count = conn.execute(sa.text(
            "SELECT count(*) FROM workflow_run_steps wrs "
            "JOIN workflow_runs wr ON wr.id = wrs.run_id "
            "WHERE wr.task_id = :id"
        ), {"id": ids["task_id"]}).scalar()
    assert ready_count == 0
    assert run_count == 1
    assert step_count == 1
    assert publisher.calls == []
    assert sqs.sent == []


@pytest.mark.asyncio
async def test_seeding_plan_activated_doesnt_dispatch_inline(
    truncate: None,
    engine: Engine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Manually insert a ``plan.activated`` event for a drafting plan,
    then call the dispatcher: the gate now passes and the task
    dispatches. This is the D.6 hand-off contract — the consumer's
    re-evaluation pass calls ``dispatch_task`` after seeing
    ``plan.activated``, and the gate must see the freshly-active state.
    """
    wv_id = _seed_wf_author(engine)
    ids = _seed_drafting_plan_with_task(engine, wv_id)

    # Drafting plan has no run yet — confirm before activation.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, plan_id, payload) "
            "VALUES ('plan', 'activated', :p, '{}'::jsonb)"
        ), {"p": ids["plan_id"]})

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
    body = json.loads(sqs.sent[0]["MessageBody"])
    assert body["task_id"] == str(ids["task_id"])
