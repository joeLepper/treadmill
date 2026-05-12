"""Integration tests for the ``--dev`` flag on POST /plans (D.10).

The dev flag is a fully-local-only fast-path for intent-only Scenario 2
submissions: in ``TREADMILL_DEPLOYMENT_MODE=fully_local`` environments
the API skips the standard ``wf-plan`` PR-merge gate, emits
``PlanActivated`` inline, and spawns an implicit single ``wf-author``
task with the intent as its description. Outside fully_local mode
(dev_local, fully_remote) the flag is ignored with a ``logging.warning``
so production traffic never accidentally side-steps planning.

Three of the four tests below issue live HTTP against the running API
(skipped unless ``TREADMILL_INTEGRATION=1``); the non-local-mode test
uses an in-process FastAPI ``TestClient`` with the ``get_settings``
dependency overridden so it does not need the live substrate at all.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from treadmill_api.config import DeploymentMode, Settings, get_settings


INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"


DEFAULT_API_URL = "http://localhost:8088"
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)


@pytest.fixture(scope="module")
def api_url() -> str:
    return os.environ.get("TREADMILL_API_URL", DEFAULT_API_URL)


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = sa.create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def migrations_applied(database_url: str) -> None:
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


@pytest.fixture(scope="module")
def client(api_url: str) -> Iterator[httpx.Client]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    with httpx.Client(base_url=api_url, timeout=10.0) as c:
        yield c


_TEST_TABLES = (
    "plans",
    "tasks",
    "task_prs",
    "task_dependencies",
    "task_validations",
    "workflow_runs",
    "workflow_run_steps",
    "events",
    "workflows",
    "workflow_versions",
    "workflow_version_steps",
    "roles",
    "skills",
    "hooks",
)


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE TABLE "
                + ", ".join(_TEST_TABLES)
                + " RESTART IDENTITY CASCADE"
            )
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE TABLE "
                + ", ".join(_TEST_TABLES)
                + " RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def seed_wf_author(engine: Engine) -> Iterator[None]:
    """Register a wf-author workflow + a v1 row + a single ``author`` step
    + a role-author role — the dev fast-path resolves and dispatches a
    one-task wf-author run, so the workflow must exist or the request
    400s. Mirrors the fixture in ``test_integration_plans_router.py``.
    """
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO workflows (id) VALUES ('wf-author')"))
        wv_id = conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES ('wf-author', 1) RETURNING id"
        )).scalar()
        conn.execute(sa.text(
            "INSERT INTO roles (id, model, system_prompt) "
            "VALUES ('role-author', 'claude', '')"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_version_steps "
            "(workflow_version_id, step_index, step_name, role_id) "
            "VALUES (:wv, 0, 'author', 'role-author')"
        ), {"wv": wv_id})
    yield


# Plan-doc template reused for the doc-content no-op test.
_PLAN_DOC_TEMPLATE = """# Plan: Test

## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "First task"
    workflow: wf-author
    intent: First task description
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: "tests pass"
```
"""


# ── Live-API integration tests (TREADMILL_INTEGRATION=1) ──────────────────────


@pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)
@pytest.mark.usefixtures("migrations_applied")
def test_dev_intent_only_creates_active_plan_with_task(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
    engine: Engine,
) -> None:
    """Fully-local-mode dev fast-path: an intent-only POST /plans with
    ``dev=true`` returns an active plan AND an implicit wf-author task
    with the intent as the description, all in the same transaction."""
    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "intent": "fix the navbar bug",
            "dev": True,
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    plan_id = plan["id"]
    assert plan["derived_status"] == "active"

    # Implicit task exists and uses wf-author + the intent as description.
    tasks_resp = client.get(f"/api/v1/plans/{plan_id}/tasks")
    assert tasks_resp.status_code == 200
    tasks = tasks_resp.json()
    assert len(tasks) == 1
    [task] = tasks
    assert task["description"] == "fix the navbar bug"
    assert task["title"].startswith("fix the navbar bug")

    # Verify the lifecycle event sequence: registered → activated →
    # task.registered (the dispatch flow also emits step.ready but we
    # only assert on the plan/task pair here).
    with engine.connect() as conn:
        plan_actions = [
            r.action for r in conn.execute(
                sa.text(
                    "SELECT action FROM events "
                    "WHERE plan_id = :id AND entity_type = 'plan' "
                    "ORDER BY created_at"
                ),
                {"id": plan_id},
            ).all()
        ]
    assert plan_actions == ["registered", "activated"]


@pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)
@pytest.mark.usefixtures("migrations_applied")
def test_dev_with_doc_content_is_noop(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
) -> None:
    """When ``doc_content`` is present, the standard Scenario 1 path
    already produces an active plan with tasks parsed from the doc. The
    dev flag is a no-op here — same outcome regardless."""
    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "doc_path": "docs/plans/x.md",
            "doc_content": _PLAN_DOC_TEMPLATE,
            "dev": True,
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["derived_status"] == "active"

    tasks = client.get(f"/api/v1/plans/{plan['id']}/tasks").json()
    # Tasks come from the doc, not the dev fast-path; the doc has one task.
    assert len(tasks) == 1
    assert tasks[0]["title"] == "First task"
    assert tasks[0]["description"] == "First task description"


@pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)
@pytest.mark.usefixtures("migrations_applied")
def test_dev_false_intent_only_drafts(
    client: httpx.Client,
    truncate: None,
    engine: Engine,
) -> None:
    """Regression: without ``dev=true``, an intent-only submission still
    follows the standard Scenario 2 path — drafting, no tasks, no
    ``PlanActivated``. Guards against the dev branch firing on default
    submissions."""
    response = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "intent": "x", "dev": False},
    )
    assert response.status_code == 201
    plan = response.json()
    plan_id = plan["id"]
    assert plan["derived_status"] == "drafting"

    tasks = client.get(f"/api/v1/plans/{plan_id}/tasks").json()
    assert tasks == []
    with engine.connect() as conn:
        actions = [
            r.action for r in conn.execute(
                sa.text(
                    "SELECT action FROM events "
                    "WHERE plan_id = :id AND entity_type = 'plan'"
                ),
                {"id": plan_id},
            ).all()
        ]
    assert "registered" in actions
    assert "activated" not in actions


# ── In-process test for the non-local-mode warning path ───────────────────────
#
# The live API runs with TREADMILL_DEPLOYMENT_MODE=fully_local (set by
# the local adapter), so the non-fully_local path cannot be exercised
# end-to-end against the running substrate. We use an in-process
# TestClient + a dependency override on ``get_settings`` to make this
# assertion. The DB engine the TestClient uses is the same Postgres the
# live tests hit, so the INSERTs land in the same place and the
# assertion is end-to-end-equivalent for what we care about: the route
# honors ``settings.is_fully_local`` to gate the fast-path.


@pytest.mark.skipif(
    not INTEGRATION,
    reason=(
        "needs the same Postgres + migrations the rest of this file uses; "
        "kept gated alongside the other tests so `treadmill-local up` is the "
        "single setup story for this file."
    ),
)
@pytest.mark.usefixtures("migrations_applied")
def test_dev_in_non_local_mode_ignored(
    truncate: None,
    seed_wf_author: None,
    engine: Engine,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``deployment_mode`` is not ``fully_local``, ``dev=true`` is
    ignored: the plan stays drafting, no implicit task is spawned, and
    a warning is logged. Production must never silently skip planning."""
    from treadmill_api.app import create_app

    # Point the in-process app at the same Postgres + drop everything
    # else (no AWS clients, no SQS) so the lifespan handler can finish
    # without external dependencies.
    async_db_url = database_url.replace("postgresql+psycopg", "postgresql+asyncpg")
    monkeypatch.setenv("DATABASE_URL", async_db_url)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("EVENTS_TOPIC_ARN", raising=False)
    monkeypatch.delenv("EVENTS_QUEUE_URL", raising=False)
    monkeypatch.delenv("WORK_QUEUE_URL", raising=False)
    # Critical: force a non-fully_local deployment mode so the dev fast-path
    # is gated off. The default is FULLY_LOCAL, so we explicitly point at
    # ``dev_local`` to simulate the AWS-side path. Also drop any legacy
    # ``TREADMILL_LOCAL`` so the back-compat shim doesn't override us.
    monkeypatch.delenv("TREADMILL_LOCAL", raising=False)
    monkeypatch.setenv("TREADMILL_DEPLOYMENT_MODE", DeploymentMode.DEV_LOCAL.value)

    # The settings cache is process-scoped (lru_cache); reset so the
    # next get_settings() call picks up our env. Reset again afterwards.
    from treadmill_api.config import reset_settings_cache
    reset_settings_cache()
    try:
        app = create_app()

        # Override get_settings to force a non-fully_local Settings. We do
        # this explicitly rather than relying solely on the env var so the
        # test stays robust if other process-level state has cached a
        # Settings somewhere else.
        def _non_local_settings() -> Settings:
            return Settings(deployment_mode=DeploymentMode.DEV_LOCAL)

        app.dependency_overrides[get_settings] = _non_local_settings

        caplog.set_level(logging.WARNING, logger="treadmill.plans")
        with TestClient(app) as in_proc:
            response = in_proc.post(
                "/api/v1/plans",
                json={"repo": "test/repo", "intent": "x", "dev": True},
            )
        assert response.status_code == 201, response.text
        plan = response.json()
        plan_id = plan["id"]
        # Standard Scenario 2 path: drafting, no implicit task.
        assert plan["derived_status"] == "drafting"
    finally:
        reset_settings_cache()

    tasks = list_tasks_for_plan(engine, plan_id)
    assert tasks == []
    with engine.connect() as conn:
        actions = [
            r.action for r in conn.execute(
                sa.text(
                    "SELECT action FROM events "
                    "WHERE plan_id = :id AND entity_type = 'plan'"
                ),
                {"id": uuid.UUID(plan_id)},
            ).all()
        ]
    assert "registered" in actions
    assert "activated" not in actions

    # Warning surfaced via the plans router's logger.
    assert any(
        "dev flag ignored" in record.getMessage()
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


def list_tasks_for_plan(engine: Engine, plan_id: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            {"id": r.id, "title": r.title}
            for r in conn.execute(
                sa.text("SELECT id, title FROM tasks WHERE plan_id = :id"),
                {"id": uuid.UUID(plan_id)},
            ).all()
        ]
