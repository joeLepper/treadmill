"""Unit tests for the steps router's ADR-0022 fields.

The integration test (``test_integration_steps_router.py``) exercises
the endpoint against live Postgres. These unit tests cover the
ADR-0022 additions — ``output_kind`` propagation on the role block +
``pr_number`` derivation from ``task_prs`` — via the shape contracts
of the response model and a focused integration test that's skipped
without the substrate.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from treadmill_api.routers.steps import WorkerContextResponse, _RoleBlock


def test_role_block_shape_includes_output_kind() -> None:
    """Per ADR-0022 — the role block in the worker context response
    includes ``output_kind``. A worker decoding the response reads
    this to pick its dispatch handler."""
    fields = _RoleBlock.model_fields
    assert "output_kind" in fields, (
        "ADR-0022 requires the steps router's role block to carry "
        "``output_kind``; the worker dispatches on this field"
    )


def test_worker_context_response_includes_pr_number() -> None:
    """Per ADR-0022 — the top-level response carries ``pr_number``
    (nullable). Required for review-kind handlers; the worker's
    dispatch raises ``MissingContextError`` when a review-kind step
    sees ``None`` here."""
    fields = WorkerContextResponse.model_fields
    assert "pr_number" in fields, (
        "ADR-0022 requires the steps router response to carry "
        "``pr_number``; review-kind handlers need it"
    )
    # The default is None — a task with no PR row stays unset.
    assert fields["pr_number"].default is None


# ── Integration tests (live DB) ─────────────────────────────────────────────


INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
_integration = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


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


@pytest.fixture(scope="module", autouse=True)
def _migrations_applied(database_url: str) -> None:
    if not INTEGRATION:
        return
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir, env=env, check=True,
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
    "events", "workflow_run_steps", "workflow_runs", "task_prs",
    "task_dependencies", "tasks", "plans", "workflow_version_steps",
    "workflow_versions", "workflows", "role_skills", "role_hooks",
    "skills", "hooks", "roles", "event_triggers",
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


@_integration
def test_get_step_context_returns_output_kind_on_role_block(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """The role block on a step context response carries the role's
    declared ``output_kind`` (per ADR-0022). The worker reads this to
    pick its dispatch handler."""
    client.post("/api/v1/roles", json={
        "id": "role-reviewer-test", "model": "claude",
        "system_prompt": "be a reviewer",
        "output_kind": "review",
        "skills": [], "hooks": [],
    })
    client.post("/api/v1/workflows", json={"id": "wf-review-test"})
    client.post(
        "/api/v1/workflows/wf-review-test/versions",
        json={"steps": [{"name": "review", "role_id": "role-reviewer-test"}]},
    )
    plan = client.post("/api/v1/plans", json={
        "repo": "ok/repo", "intent": "review it",
    }).json()
    task = client.post("/api/v1/tasks", json={
        "plan_id": plan["id"], "title": "T", "workflow": "wf-review-test",
    }).json()
    with engine.connect() as conn:
        step_id = conn.execute(
            sa.text(
                "SELECT s.id FROM workflow_run_steps s "
                "JOIN workflow_runs r ON r.id = s.run_id "
                "WHERE r.task_id = :t LIMIT 1"
            ),
            {"t": task["id"]},
        ).scalar()
    resp = client.get(f"/api/v1/steps/{step_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"]["output_kind"] == "review"
    # No PR yet — pr_number is null.
    assert body.get("pr_number") is None


@_integration
def test_get_step_context_propagates_pr_number_from_task_prs(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """When the task has opened a PR (``task_prs`` row exists), the
    step context's ``pr_number`` reflects it. Review-kind handlers
    rely on this field being populated."""
    client.post("/api/v1/roles", json={
        "id": "role-code-test", "model": "claude",
        "system_prompt": "be a coder",
        "output_kind": "code",
        "skills": [], "hooks": [],
    })
    client.post("/api/v1/workflows", json={"id": "wf-code-test"})
    client.post(
        "/api/v1/workflows/wf-code-test/versions",
        json={"steps": [{"name": "author", "role_id": "role-code-test"}]},
    )
    plan = client.post("/api/v1/plans", json={
        "repo": "ok/repo", "intent": "ship it",
    }).json()
    task = client.post("/api/v1/tasks", json={
        "plan_id": plan["id"], "title": "T", "workflow": "wf-code-test",
    }).json()
    # Insert a task_prs bridge row manually; the dispatcher would
    # normally do this after a worker opens a PR, but the dispatcher
    # path is event-driven and we want the test focused on the
    # router's read-side.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id, branch) "
                "VALUES (:repo, :pr, :tid, :br)"
            ),
            {"repo": "ok/repo", "pr": 42, "tid": task["id"], "br": "task/x"},
        )
        step_id = conn.execute(
            sa.text(
                "SELECT s.id FROM workflow_run_steps s "
                "JOIN workflow_runs r ON r.id = s.run_id "
                "WHERE r.task_id = :t LIMIT 1"
            ),
            {"t": task["id"]},
        ).scalar()
    resp = client.get(f"/api/v1/steps/{step_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pr_number"] == 42
