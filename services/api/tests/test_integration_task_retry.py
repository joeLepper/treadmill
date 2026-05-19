"""Integration tests for POST /api/v1/tasks/{task_id}/retry (ADR-0046).

Covers the full server-side behavior:
  * 404 for unknown task.
  * 409 when no retryable workflow can be inferred and none passed.
  * 409 when cap is hit and force_bypass_cap is not set.
  * Happy path: dedup row cleared, task.retry event persisted,
    new workflow_run created, step.ready published.
  * force_bypass_cap=true bypasses the cap and still creates a run.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest services/api/tests/test_integration_task_retry.py
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
from sqlalchemy.engine import Connection, Engine

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


DEFAULT_API_URL = "http://localhost:8088"
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
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
    "schedules",
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


# ── Seed helpers ──────────────────────────────────────────────────────────────


def _make_plan(engine: Engine, repo: str = "test/retry-repo") -> uuid.UUID:
    with engine.begin() as conn:
        row = conn.execute(
            sa.text("INSERT INTO plans (repo) VALUES (:repo) RETURNING id"),
            {"repo": repo},
        ).one()
    return row.id


def _make_role(conn: Connection, role_id: str = "role-feedback") -> str:
    conn.execute(
        sa.text(
            "INSERT INTO roles (id, model, system_prompt, output_kind) "
            "VALUES (:id, 'claude', '', 'code') ON CONFLICT (id) DO NOTHING"
        ),
        {"id": role_id},
    )
    return role_id


def _make_workflow_version(
    conn: Connection,
    slug: str,
    role_id: str = "role-feedback",
) -> uuid.UUID:
    """Idempotent: register workflow + version + one step."""
    existing = conn.execute(
        sa.text(
            "SELECT wv.id FROM workflow_versions wv "
            "WHERE wv.workflow_id = :s AND wv.version = 1"
        ),
        {"s": slug},
    ).first()
    if existing:
        return existing.id

    conn.execute(
        sa.text(
            "INSERT INTO workflows (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": slug},
    )
    version_id = conn.execute(
        sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES (:s, 1) RETURNING id"
        ),
        {"s": slug},
    ).scalar()
    # Seed one step so _create_and_publish_run can dispatch.
    conn.execute(
        sa.text(
            "INSERT INTO workflow_version_steps "
            "(workflow_version_id, step_index, step_name, role_id) "
            "VALUES (:wv, 0, 'run', :role)"
        ),
        {"wv": version_id, "role": role_id},
    )
    return version_id


def _make_task(
    engine: Engine,
    plan_id: uuid.UUID,
    *,
    repo: str = "test/retry-repo",
    workflow_slug: str = "wf-author",
) -> uuid.UUID:
    with engine.begin() as conn:
        _make_role(conn)
        wv_id = _make_workflow_version(conn, workflow_slug)
        task_id = conn.execute(
            sa.text(
                "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
                "VALUES (:plan_id, :repo, 'retry test task', :wv) RETURNING id"
            ),
            {"plan_id": plan_id, "repo": repo, "wv": wv_id},
        ).scalar()
    return task_id


def _make_workflow_run(
    engine: Engine,
    task_id: uuid.UUID,
    workflow_slug: str,
    *,
    repo: str = "test/retry-repo",
    trigger: str = "registered",
    step_status: str = "failed",
) -> uuid.UUID:
    """Seed a workflow run with one step at the given status."""
    with engine.begin() as conn:
        _make_role(conn)
        wv_id = _make_workflow_version(conn, workflow_slug)
        run_id = conn.execute(
            sa.text(
                "INSERT INTO workflow_runs (task_id, workflow_version_id, trigger) "
                "VALUES (:t, :wv, :trig) RETURNING id"
            ),
            {"t": task_id, "wv": wv_id, "trig": trigger},
        ).scalar()
        conn.execute(
            sa.text(
                "INSERT INTO workflow_run_steps "
                "(run_id, step_index, step_name, role_id, status) "
                "VALUES (:r, 0, 'run', 'role-feedback', :s)"
            ),
            {"r": run_id, "s": step_status},
        )
    return run_id


def _make_dedup_row(
    engine: Engine,
    *,
    dedup_key: str,
    workflow_run_id: uuid.UUID,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO workflow_dispatch_dedup (dedup_key, workflow_run_id) "
                "VALUES (:key, :run_id)"
            ),
            {"key": dedup_key, "run_id": workflow_run_id},
        )


def _dedup_row_exists(engine: Engine, dedup_key: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT 1 FROM workflow_dispatch_dedup WHERE dedup_key = :key"
            ),
            {"key": dedup_key},
        ).first()
    return row is not None


def _count_workflow_runs(
    engine: Engine, task_id: uuid.UUID, workflow_slug: str
) -> int:
    with engine.connect() as conn:
        count = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM workflow_runs wr "
                "JOIN workflow_versions wv ON wv.id = wr.workflow_version_id "
                "WHERE wr.task_id = :t AND wv.workflow_id = :slug"
            ),
            {"t": task_id, "slug": workflow_slug},
        ).scalar()
    return count or 0


def _get_retry_event(engine: Engine, task_id: uuid.UUID) -> dict | None:
    import json

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT payload FROM events "
                "WHERE task_id = :t AND entity_type = 'task' AND action = 'retry' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": task_id},
        ).first()
    if row is None:
        return None
    p = row.payload
    return json.loads(p) if isinstance(p, str) else p


def _get_step_ready_run_ids(engine: Engine, task_id: uuid.UUID) -> list[uuid.UUID]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT run_id FROM events "
                "WHERE task_id = :t AND entity_type = 'step' AND action = 'ready'"
            ),
            {"t": task_id},
        ).fetchall()
    return [r.run_id for r in rows]


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestTaskRetry:
    def test_404_for_unknown_task(
        self, client: httpx.Client, truncate: None
    ) -> None:
        resp = client.post(
            f"/api/v1/tasks/{uuid.uuid4()}/retry",
            json={"reason": "testing"},
        )
        assert resp.status_code == 404
        assert "task not found" in resp.json()["detail"]

    def test_409_when_no_retryable_workflow(
        self, client: httpx.Client, engine: Engine, truncate: None
    ) -> None:
        """A brand-new task with no runs → infer_retry_workflow returns None."""
        plan_id = _make_plan(engine)
        task_id = _make_task(engine, plan_id)
        resp = client.post(
            f"/api/v1/tasks/{task_id}/retry",
            json={"reason": "nudge"},
        )
        assert resp.status_code == 409
        assert "no retryable workflow" in resp.json()["detail"]

    def test_409_when_cap_hit_without_force(
        self, client: httpx.Client, engine: Engine, truncate: None
    ) -> None:
        """wf-feedback has a cap of 5; seeding 5 runs should trigger 409."""
        plan_id = _make_plan(engine)
        task_id = _make_task(engine, plan_id)
        for _ in range(5):
            _make_workflow_run(engine, task_id, "wf-feedback", step_status="failed")
        resp = client.post(
            f"/api/v1/tasks/{task_id}/retry",
            json={"workflow_id": "wf-feedback", "reason": "nudge"},
        )
        assert resp.status_code == 409
        assert "cap reached" in resp.json()["detail"]
        assert "force_bypass_cap" in resp.json()["detail"]

    def test_happy_path(
        self, client: httpx.Client, engine: Engine, truncate: None
    ) -> None:
        """Full retry: dedup cleared, task.retry event exists, new run created,
        step.ready published."""
        repo = "test/retry-repo"
        plan_id = _make_plan(engine, repo)
        task_id = _make_task(engine, plan_id, repo=repo)

        # Seed: an initial wf-author run (the one referenced in the dedup key).
        author_run_id = _make_workflow_run(
            engine, task_id, "wf-author",
            repo=repo, trigger="registered", step_status="completed",
        )

        # Seed: a wf-feedback run that failed (most recent run, inferred target).
        feedback_run_id = _make_workflow_run(
            engine, task_id, "wf-feedback",
            repo=repo, trigger="self:wf-author-fail", step_status="failed",
        )

        # Seed the dedup row that blocks a new wf-feedback dispatch.
        dedup_key = f"wf-feedback:{repo}:author-fail-run={author_run_id}"
        _make_dedup_row(engine, dedup_key=dedup_key, workflow_run_id=feedback_run_id)
        assert _dedup_row_exists(engine, dedup_key)

        runs_before = _count_workflow_runs(engine, task_id, "wf-feedback")

        resp = client.post(
            f"/api/v1/tasks/{task_id}/retry",
            json={"reason": "prompt improved; retrying wf-feedback"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "workflow_run_id" in body
        new_run_id = uuid.UUID(body["workflow_run_id"])
        assert new_run_id != feedback_run_id

        # Dedup row must be gone.
        assert not _dedup_row_exists(engine, dedup_key)

        # A new wf-feedback run must exist.
        runs_after = _count_workflow_runs(engine, task_id, "wf-feedback")
        assert runs_after == runs_before + 1

        # task.retry audit event must be persisted.
        event = _get_retry_event(engine, task_id)
        assert event is not None
        assert event["workflow_id"] == "wf-feedback"
        assert event["reason"] == "prompt improved; retrying wf-feedback"
        assert event["bypassed_cap"] is False

        # step.ready event must exist for the new run.
        ready_run_ids = _get_step_ready_run_ids(engine, task_id)
        assert new_run_id in ready_run_ids

    def test_force_bypass_cap_creates_run(
        self, client: httpx.Client, engine: Engine, truncate: None
    ) -> None:
        """force_bypass_cap=true allows a 6th wf-feedback run."""
        plan_id = _make_plan(engine)
        task_id = _make_task(engine, plan_id)
        for _ in range(5):
            _make_workflow_run(engine, task_id, "wf-feedback", step_status="failed")

        resp = client.post(
            f"/api/v1/tasks/{task_id}/retry",
            json={
                "workflow_id": "wf-feedback",
                "reason": "cap hit during prompt iteration; current prompts will succeed",
                "force_bypass_cap": True,
            },
        )
        assert resp.status_code == 201, resp.text
        assert "workflow_run_id" in resp.json()

        runs_after = _count_workflow_runs(engine, task_id, "wf-feedback")
        assert runs_after == 6

        event = _get_retry_event(engine, task_id)
        assert event is not None
        assert event["bypassed_cap"] is True

    def test_explicit_workflow_id_overrides_inference(
        self, client: httpx.Client, engine: Engine, truncate: None
    ) -> None:
        """Explicit workflow_id is respected even when inference would pick another."""
        plan_id = _make_plan(engine)
        task_id = _make_task(engine, plan_id)
        # Most recent run is wf-feedback (inference would return that).
        _make_workflow_run(engine, task_id, "wf-feedback", step_status="failed")
        # But operator explicitly asks for wf-ci-fix.
        _make_workflow_run(engine, task_id, "wf-ci-fix", step_status="failed")

        resp = client.post(
            f"/api/v1/tasks/{task_id}/retry",
            json={"workflow_id": "wf-ci-fix", "reason": "re-running ci fix"},
        )
        assert resp.status_code == 201, resp.text

        event = _get_retry_event(engine, task_id)
        assert event is not None
        assert event["workflow_id"] == "wf-ci-fix"
