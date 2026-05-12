"""Integration tests for the Day 4 router surface: tasks, roles, workflows,
skills, hooks, event_triggers.

Each section covers create + read + 404 + 409 (where applicable). Skipped
by default; opt in with ``TREADMILL_INTEGRATION=1``.
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

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
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
def engine(database_url: str) -> Engine:
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


_TEST_TABLES = (
    "plans", "tasks", "task_prs", "task_dependencies",
    "workflow_runs", "workflow_run_steps", "events",
    "event_triggers",
    "workflows", "workflow_versions", "workflow_version_steps",
    "roles", "skills", "hooks",
)


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    def _do_truncate() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(_TEST_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )
    _do_truncate()
    yield
    _do_truncate()


# ── Skills ───────────────────────────────────────────────────────────────────


class TestSkills:
    def test_create_and_get(self, client: httpx.Client, truncate: None) -> None:
        body = {"id": "skill-x", "name": "Skill X", "content": "do X"}
        create = client.post("/api/v1/skills", json=body)
        assert create.status_code == 201, create.text
        assert create.json()["id"] == "skill-x"
        get = client.get("/api/v1/skills/skill-x")
        assert get.status_code == 200
        assert get.json()["name"] == "Skill X"

    def test_list(self, client: httpx.Client, truncate: None) -> None:
        for slug in ("skill-a", "skill-b"):
            client.post("/api/v1/skills", json={"id": slug, "name": slug, "content": "x"})
        listed = client.get("/api/v1/skills").json()
        assert {s["id"] for s in listed} == {"skill-a", "skill-b"}

    def test_duplicate_returns_409(self, client: httpx.Client, truncate: None) -> None:
        client.post("/api/v1/skills", json={"id": "skill-dup", "name": "n", "content": "c"})
        dup = client.post("/api/v1/skills", json={"id": "skill-dup", "name": "n", "content": "c"})
        assert dup.status_code == 409

    def test_get_missing_returns_404(self, client: httpx.Client, truncate: None) -> None:
        assert client.get("/api/v1/skills/skill-nope").status_code == 404


# ── Hooks ────────────────────────────────────────────────────────────────────


class TestHooks:
    def test_create_and_get(self, client: httpx.Client, truncate: None) -> None:
        body = {
            "id": "hook-x", "name": "Hook X", "event": "PreToolUse",
            "matcher": "Bash", "command": "echo hello",
        }
        create = client.post("/api/v1/hooks", json=body)
        assert create.status_code == 201
        get = client.get("/api/v1/hooks/hook-x").json()
        assert get["event"] == "PreToolUse"
        assert get["matcher"] == "Bash"

    def test_create_without_matcher(self, client: httpx.Client, truncate: None) -> None:
        """Matcher is optional."""
        body = {"id": "hook-y", "name": "Y", "event": "Stop", "command": "x"}
        assert client.post("/api/v1/hooks", json=body).status_code == 201

    def test_duplicate_returns_409(self, client: httpx.Client, truncate: None) -> None:
        b = {"id": "hook-dup", "name": "n", "event": "x", "command": "c"}
        client.post("/api/v1/hooks", json=b)
        assert client.post("/api/v1/hooks", json=b).status_code == 409


# ── Roles ────────────────────────────────────────────────────────────────────


class TestRoles:
    def test_create_role_with_skills_and_hooks(
        self, client: httpx.Client, truncate: None
    ) -> None:
        client.post("/api/v1/skills", json={"id": "skill-s1", "name": "n", "content": "c"})
        client.post("/api/v1/skills", json={"id": "skill-s2", "name": "n", "content": "c"})
        client.post("/api/v1/hooks", json={"id": "hook-h1", "name": "n", "event": "x", "command": "c"})
        body = {
            "id": "role-x", "model": "claude", "system_prompt": "be a coder",
            "output_kind": "code",
            "skills": ["skill-s1", "skill-s2"],
            "hooks": ["hook-h1"],
        }
        create = client.post("/api/v1/roles", json=body)
        assert create.status_code == 201, create.text
        # Order is preserved.
        assert create.json()["skills"] == ["skill-s1", "skill-s2"]
        assert create.json()["hooks"] == ["hook-h1"]

    def test_create_role_with_unknown_skill_400s(
        self, client: httpx.Client, truncate: None
    ) -> None:
        body = {
            "id": "role-y", "model": "claude", "system_prompt": "x",
            "output_kind": "code",
            "skills": ["skill-missing"],
        }
        resp = client.post("/api/v1/roles", json=body)
        assert resp.status_code == 400
        assert "skill-missing" in resp.json()["detail"]

    def test_get_role_returns_skills_and_hooks_in_order(
        self, client: httpx.Client, truncate: None
    ) -> None:
        for slug in ("skill-a", "skill-b", "skill-c"):
            client.post("/api/v1/skills", json={"id": slug, "name": "n", "content": "c"})
        body = {
            "id": "role-ordered", "model": "claude", "system_prompt": "x",
            "output_kind": "code",
            "skills": ["skill-c", "skill-a", "skill-b"],
        }
        client.post("/api/v1/roles", json=body)
        got = client.get("/api/v1/roles/role-ordered").json()
        assert got["skills"] == ["skill-c", "skill-a", "skill-b"]


# ── Workflows + Versions ─────────────────────────────────────────────────────


class TestWorkflows:
    def _seed_role(self, client: httpx.Client, role_id: str = "role-z") -> None:
        client.post("/api/v1/roles", json={
            "id": role_id, "model": "claude", "system_prompt": "x",
            "output_kind": "code",
        })

    def test_create_workflow(self, client: httpx.Client, truncate: None) -> None:
        resp = client.post("/api/v1/workflows", json={"id": "wf-x", "description": "x"})
        assert resp.status_code == 201
        assert resp.json()["latest_version"] is None

    def test_create_version_assigns_sequential_versions(
        self, client: httpx.Client, truncate: None
    ) -> None:
        self._seed_role(client)
        client.post("/api/v1/workflows", json={"id": "wf-vers"})
        v1 = client.post(
            "/api/v1/workflows/wf-vers/versions",
            json={"steps": [{"name": "author", "role_id": "role-z"}]},
        )
        assert v1.status_code == 201
        assert v1.json()["version"] == 1
        v2 = client.post(
            "/api/v1/workflows/wf-vers/versions",
            json={"steps": [{"name": "author", "role_id": "role-z"}]},
        )
        assert v2.json()["version"] == 2

    def test_create_version_with_unknown_role_400s(
        self, client: httpx.Client, truncate: None
    ) -> None:
        client.post("/api/v1/workflows", json={"id": "wf-bad-role"})
        resp = client.post(
            "/api/v1/workflows/wf-bad-role/versions",
            json={"steps": [{"name": "author", "role_id": "role-missing"}]},
        )
        assert resp.status_code == 400

    def test_create_version_for_unknown_workflow_404s(
        self, client: httpx.Client, truncate: None
    ) -> None:
        self._seed_role(client)
        resp = client.post(
            "/api/v1/workflows/wf-nope/versions",
            json={"steps": [{"name": "author", "role_id": "role-z"}]},
        )
        assert resp.status_code == 404

    def test_workflow_response_includes_latest_version(
        self, client: httpx.Client, truncate: None
    ) -> None:
        self._seed_role(client)
        client.post("/api/v1/workflows", json={"id": "wf-latest"})
        client.post(
            "/api/v1/workflows/wf-latest/versions",
            json={"steps": [{"name": "author", "role_id": "role-z"}]},
        )
        got = client.get("/api/v1/workflows/wf-latest").json()
        assert got["latest_version"] == 1


# ── Tasks ────────────────────────────────────────────────────────────────────


class TestTasks:
    def _seed_workflow(self, client: httpx.Client) -> None:
        client.post("/api/v1/roles", json={
            "id": "role-author", "model": "claude", "system_prompt": "x",
            "output_kind": "code",
        })
        client.post("/api/v1/workflows", json={"id": "wf-author"})
        client.post(
            "/api/v1/workflows/wf-author/versions",
            json={"steps": [{"name": "author", "role_id": "role-author"}]},
        )

    def test_create_task_under_existing_plan(
        self, client: httpx.Client, truncate: None
    ) -> None:
        self._seed_workflow(client)
        plan = client.post(
            "/api/v1/plans",
            json={"repo": "test/repo", "intent": "x"},
        ).json()
        resp = client.post(
            "/api/v1/tasks",
            json={"plan_id": plan["id"], "title": "T", "workflow": "wf-author"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["plan_id"] == plan["id"]
        assert body["repo"] == "test/repo"
        # Per ADR-0010, the dispatcher auto-creates a WorkflowRun on task
        # creation; derived_status resolves to "<workflow>: executing".
        assert body["derived_status"] == "wf-author: executing"

    def test_create_task_with_unknown_plan_400s(
        self, client: httpx.Client, truncate: None
    ) -> None:
        self._seed_workflow(client)
        resp = client.post(
            "/api/v1/tasks",
            json={
                "plan_id": str(uuid.uuid4()), "title": "T", "workflow": "wf-author",
            },
        )
        assert resp.status_code == 400

    def test_create_task_with_unknown_workflow_400s(
        self, client: httpx.Client, truncate: None
    ) -> None:
        plan = client.post(
            "/api/v1/plans", json={"repo": "test/repo", "intent": "x"},
        ).json()
        resp = client.post(
            "/api/v1/tasks",
            json={"plan_id": plan["id"], "title": "T", "workflow": "wf-nope"},
        )
        assert resp.status_code == 400

    def test_get_task_returns_derived_status(
        self, client: httpx.Client, truncate: None
    ) -> None:
        self._seed_workflow(client)
        plan = client.post("/api/v1/plans", json={"repo": "test/repo", "intent": "x"}).json()
        task = client.post(
            "/api/v1/tasks",
            json={"plan_id": plan["id"], "title": "T", "workflow": "wf-author"},
        ).json()
        got = client.get(f"/api/v1/tasks/{task['id']}").json()
        assert got["derived_status"] == "wf-author: executing"

    def test_get_missing_task_404s(self, client: httpx.Client, truncate: None) -> None:
        assert client.get(f"/api/v1/tasks/{uuid.uuid4()}").status_code == 404

    def test_list_tasks_filters_by_plan_id(
        self, client: httpx.Client, truncate: None
    ) -> None:
        self._seed_workflow(client)
        p1 = client.post("/api/v1/plans", json={"repo": "r1", "intent": "x"}).json()
        p2 = client.post("/api/v1/plans", json={"repo": "r2", "intent": "x"}).json()
        for plan in (p1, p2):
            client.post(
                "/api/v1/tasks",
                json={"plan_id": plan["id"], "title": "T", "workflow": "wf-author"},
            )
        only_p1 = client.get(f"/api/v1/tasks?plan_id={p1['id']}").json()
        assert len(only_p1) == 1
        assert only_p1[0]["plan_id"] == p1["id"]

    def test_create_task_persists_task_registered_event(
        self, client: httpx.Client, truncate: None, engine: Engine,
    ) -> None:
        """A.6 — POST /tasks emits exactly one ``task.registered`` Event
        row whose payload carries the task's repo + title + workflow
        version + plan id."""
        self._seed_workflow(client)
        plan = client.post(
            "/api/v1/plans", json={"repo": "test/repo", "intent": "x"},
        ).json()
        task = client.post(
            "/api/v1/tasks",
            json={
                "plan_id": plan["id"], "title": "A new feature",
                "workflow": "wf-author",
            },
        ).json()
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT action, payload FROM events "
                    "WHERE task_id = :t AND entity_type = 'task'"
                ),
                {"t": task["id"]},
            ).all()
        registered = [r for r in rows if r.action == "registered"]
        assert len(registered) == 1
        payload = registered[0].payload
        assert payload["repo"] == "test/repo"
        assert payload["title"] == "A new feature"
        assert payload["plan_id"] == plan["id"]


# ── EventTriggers ────────────────────────────────────────────────────────────


class TestEventTriggers:
    def _seed_workflow(self, client: httpx.Client) -> None:
        client.post("/api/v1/workflows", json={"id": "wf-t"})

    def test_create_trigger(self, client: httpx.Client, truncate: None) -> None:
        self._seed_workflow(client)
        resp = client.post(
            "/api/v1/event-triggers",
            json={
                "repo": "test/repo", "event_type": "pr_opened", "workflow_id": "wf-t",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["enabled"] is True

    def test_create_trigger_with_unknown_workflow_400s(
        self, client: httpx.Client, truncate: None
    ) -> None:
        resp = client.post(
            "/api/v1/event-triggers",
            json={
                "repo": "test/repo", "event_type": "pr_opened", "workflow_id": "wf-nope",
            },
        )
        assert resp.status_code == 400

    def test_duplicate_trigger_for_same_repo_event_409s(
        self, client: httpx.Client, truncate: None
    ) -> None:
        self._seed_workflow(client)
        body = {"repo": "test/repo", "event_type": "pr_opened", "workflow_id": "wf-t"}
        client.post("/api/v1/event-triggers", json=body)
        assert client.post("/api/v1/event-triggers", json=body).status_code == 409

    def test_list_triggers_filtered_by_event_type(
        self, client: httpx.Client, truncate: None
    ) -> None:
        self._seed_workflow(client)
        client.post("/api/v1/event-triggers", json={
            "repo": "r1", "event_type": "pr_opened", "workflow_id": "wf-t",
        })
        client.post("/api/v1/event-triggers", json={
            "repo": "r2", "event_type": "pr_merged", "workflow_id": "wf-t",
        })
        got = client.get("/api/v1/event-triggers?event_type=pr_opened").json()
        assert len(got) == 1
        assert got[0]["repo"] == "r1"

    def test_global_trigger_with_null_repo(
        self, client: httpx.Client, truncate: None
    ) -> None:
        """A trigger with repo=null applies to all repos per ADR-0010 follow-up."""
        self._seed_workflow(client)
        resp = client.post(
            "/api/v1/event-triggers",
            json={"event_type": "pr_opened", "workflow_id": "wf-t"},
        )
        assert resp.status_code == 201
        assert resp.json()["repo"] is None
