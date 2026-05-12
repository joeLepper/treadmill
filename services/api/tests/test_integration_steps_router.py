"""Integration tests for the worker-facing steps router.

The steps router computes the worker context server-side: a single
``GET /api/v1/steps/{id}`` returns the step + run + task + plan + role
(with resolved skills + hooks). These tests assert that join logic.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest tests/test_integration_steps_router.py
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


def _seed_full_graph(client: httpx.Client) -> tuple[str, str]:
    """Use the public API to seed a complete graph and return the (step_id,
    role_id). Going through the API exercises the dispatcher too — we end
    up with a pending step ready for worker pickup."""
    client.post("/api/v1/skills", json={
        "id": "skill-author", "name": "authoring",
        "content": "skill content for author",
    })
    client.post("/api/v1/hooks", json={
        "id": "hook-pre", "name": "pre-author",
        "event": "PreToolUse", "matcher": None, "command": "echo go",
    })
    client.post("/api/v1/roles", json={
        "id": "role-author", "model": "claude-opus-4-7",
        "system_prompt": "be a coder",
        "skills": ["skill-author"], "hooks": ["hook-pre"],
    })
    client.post("/api/v1/workflows", json={"id": "wf-author"})
    client.post(
        "/api/v1/workflows/wf-author/versions",
        json={"steps": [{"name": "author", "role_id": "role-author"}]},
    )

    plan = client.post("/api/v1/plans", json={
        "repo": "steps-test/repo", "intent": "build it",
    }).json()
    task = client.post("/api/v1/tasks", json={
        "plan_id": plan["id"], "title": "T",
        "workflow": "wf-author", "description": "do the thing",
    }).json()

    # Find the dispatched step.
    return _await_step(client, task["id"]), "role-author"


def _await_step(client: httpx.Client, task_id: str) -> str:
    """Poll the events endpoint indirectly: list tasks and inspect
    derived_status. Once the dispatcher has run, query the DB for the
    step. Works because the dispatcher runs synchronously inside the
    POST /tasks request."""
    # The fastest path is just to re-fetch the task with derived_status
    # populated; the run + step ids aren't surfaced via the existing API
    # so we read them from the database directly.
    import sqlalchemy as sa  # noqa
    eng = sa.create_engine(
        os.environ.get(
            "TREADMILL_TEST_DATABASE_URL",
            "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill",
        )
    )
    with eng.connect() as conn:
        step_id = conn.execute(
            sa.text(
                "SELECT s.id FROM workflow_run_steps s "
                "JOIN workflow_runs r ON r.id = s.run_id "
                "WHERE r.task_id = :t LIMIT 1"
            ),
            {"t": task_id},
        ).scalar()
    eng.dispose()
    return str(step_id)


# ── tests ─────────────────────────────────────────────────────────────────────


def test_get_step_returns_full_worker_context(
    client: httpx.Client, truncate: None,
) -> None:
    step_id, role_id = _seed_full_graph(client)
    resp = client.get(f"/api/v1/steps/{step_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Step block
    assert body["step"]["id"] == step_id
    assert body["step"]["step_index"] == 0
    assert body["step"]["step_name"] == "author"
    assert body["step"]["role_id"] == role_id
    assert body["step"]["status"] == "pending"

    # Run block
    assert body["run"]["workflow_id"] == "wf-author"
    assert body["run"]["workflow_version"] == 1
    assert body["run"]["trigger"] == "registered"

    # Task + plan blocks
    assert body["task"]["repo"] == "steps-test/repo"
    assert body["task"]["title"] == "T"
    assert body["task"]["description"] == "do the thing"
    assert body["plan"]["intent"] == "build it"

    # Role block with resolved skills + hooks
    assert body["role"]["model"] == "claude-opus-4-7"
    assert body["role"]["system_prompt"] == "be a coder"
    # compute_tier is intentionally absent from the wire per decision #12.
    assert "compute_tier" not in body["role"]
    assert [s["id"] for s in body["role"]["skills"]] == ["skill-author"]
    assert body["role"]["skills"][0]["content"] == "skill content for author"
    assert [h["id"] for h in body["role"]["hooks"]] == ["hook-pre"]
    assert body["role"]["hooks"][0]["command"] == "echo go"


def test_get_step_404_on_unknown_id(
    client: httpx.Client, truncate: None,
) -> None:
    resp = client.get(f"/api/v1/steps/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_step_context_500s_with_clear_message_when_run_missing(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """C.3: explicit raises replace asserts on FK-implied lookups.

    Seed the graph, then break a single FK-implied invariant: delete the
    workflow_run row that the step points at, while keeping the step.
    Postgres won't allow that via vanilla DELETE (ON DELETE CASCADE would
    drop the step too), so we temporarily disable triggers to simulate the
    data-corruption scenario the explicit raise is designed to surface.
    """
    step_id, _ = _seed_full_graph(client)

    # Drop the run while keeping the step (simulating data corruption).
    with engine.begin() as conn:
        run_id = conn.execute(
            sa.text("SELECT run_id FROM workflow_run_steps WHERE id = :s"),
            {"s": step_id},
        ).scalar()
        conn.execute(sa.text("ALTER TABLE workflow_run_steps DISABLE TRIGGER ALL"))
        conn.execute(sa.text("ALTER TABLE workflow_runs DISABLE TRIGGER ALL"))
        try:
            conn.execute(
                sa.text("DELETE FROM workflow_runs WHERE id = :r"),
                {"r": run_id},
            )
        finally:
            conn.execute(sa.text("ALTER TABLE workflow_runs ENABLE TRIGGER ALL"))
            conn.execute(sa.text("ALTER TABLE workflow_run_steps ENABLE TRIGGER ALL"))

    resp = client.get(f"/api/v1/steps/{step_id}")
    assert resp.status_code == 500, resp.text
    # The error message names the missing referent so on-call can act.
    detail = resp.json()["detail"]
    assert "workflow_run" in detail
    assert str(run_id) in detail


def _seed_two_step_workflow(
    client: httpx.Client, engine: Engine,
) -> tuple[str, str, str]:
    """Seed a 2-step workflow + a task. Returns (run_id, step1_id, step2_id).

    The dispatcher persists both step rows up-front (pending) so both
    ids are available immediately; A.4's prior_steps query relies on
    that "all steps exist; only step 1 is completed" shape to drive
    the completed-only filter.
    """
    client.post("/api/v1/skills", json={
        "id": "skill-shared", "name": "shared", "content": "shared content",
    })
    client.post("/api/v1/roles", json={
        "id": "role-analyzer", "model": "claude-haiku-4-5",
        "system_prompt": "be an analyzer",
        "skills": [], "hooks": [],
    })
    client.post("/api/v1/roles", json={
        "id": "role-actor", "model": "claude-opus-4-7",
        "system_prompt": "be an actor",
        "skills": [], "hooks": [],
    })
    client.post("/api/v1/workflows", json={"id": "wf-two-step"})
    client.post(
        "/api/v1/workflows/wf-two-step/versions",
        json={"steps": [
            {"name": "analyze", "role_id": "role-analyzer"},
            {"name": "act", "role_id": "role-actor"},
        ]},
    )

    plan = client.post("/api/v1/plans", json={
        "repo": "two-step/repo", "intent": "do two things",
    }).json()
    task = client.post("/api/v1/tasks", json={
        "plan_id": plan["id"], "title": "TT",
        "workflow": "wf-two-step", "description": "two-step task",
    }).json()

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT s.id, s.step_index, s.run_id "
                "FROM workflow_run_steps s "
                "JOIN workflow_runs r ON r.id = s.run_id "
                "WHERE r.task_id = :t "
                "ORDER BY s.step_index"
            ),
            {"t": task["id"]},
        ).all()
    assert len(rows) == 2, f"expected 2 seeded steps, got {len(rows)}"
    return str(rows[0].run_id), str(rows[0].id), str(rows[1].id)


def test_get_step_prior_steps_is_empty_for_first_step(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """Step 1 of a 2-step workflow has no prior steps."""
    _run_id, step1_id, _step2_id = _seed_two_step_workflow(client, engine)
    resp = client.get(f"/api/v1/steps/{step1_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["prior_steps"] == []


def test_get_step_prior_steps_skips_incomplete_steps(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """Per ADR-0015, ``prior_steps`` returns only completed prior steps.
    Running / pending / failed prior steps don't carry usable directive
    output and would mislead the action role's prompt-composer. Until
    step 1 lands as ``completed``, step 2 sees an empty list."""
    _run_id, _step1_id, step2_id = _seed_two_step_workflow(client, engine)
    resp = client.get(f"/api/v1/steps/{step2_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["prior_steps"] == []


def test_get_step_prior_steps_returns_completed_prior_step(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """The headline A.4 case: step 1 completes with a StepOutput envelope
    in its ``output`` column; step 2's context surfaces that output.
    Worker side reads ``prior_steps[0].output['payload']['task_directive']``
    per ADR-0015's two-step convention."""
    _run_id, step1_id, step2_id = _seed_two_step_workflow(client, engine)

    envelope = {
        "summary": "classified inbound signal into a task directive",
        "decision": "plan-ready",
        "commit_sha": "deadbeefcafe1234",
        "artifacts": [],
        "payload": {
            "task_directive": {
                "summary": "Fix the nullable bug",
                "files": ["foo.py"],
                "intent": "Guard against None inputs.",
                "out_of_scope": [],
                "validation": [],
            },
        },
        "metadata": {},
    }
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE workflow_run_steps "
                "SET status = 'completed', output = CAST(:o AS jsonb), "
                "    started_at = now(), completed_at = now() "
                "WHERE id = :s"
            ),
            {"o": __import__("json").dumps(envelope), "s": step1_id},
        )

    resp = client.get(f"/api/v1/steps/{step2_id}")
    assert resp.status_code == 200, resp.text
    prior = resp.json()["prior_steps"]
    assert len(prior) == 1
    assert prior[0]["step_index"] == 0
    assert prior[0]["step_name"] == "analyze"
    assert prior[0]["role_id"] == "role-analyzer"
    assert prior[0]["status"] == "completed"
    assert prior[0]["output"]["decision"] == "plan-ready"
    assert prior[0]["output"]["commit_sha"] == "deadbeefcafe1234"
    assert (
        prior[0]["output"]["payload"]["task_directive"]["files"] == ["foo.py"]
    )


def test_get_step_prior_steps_ordered_by_step_index(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """Three-step extension of the fixture: assert ``prior_steps`` for
    step 3 returns step 1 then step 2 (ascending), regardless of
    completion order. Worker side picks by index, not insertion order."""
    # Build a 3-role / 3-step workflow.
    client.post("/api/v1/roles", json={
        "id": "role-r1", "model": "m", "system_prompt": "p",
        "skills": [], "hooks": [],
    })
    client.post("/api/v1/roles", json={
        "id": "role-r2", "model": "m", "system_prompt": "p",
        "skills": [], "hooks": [],
    })
    client.post("/api/v1/roles", json={
        "id": "role-r3", "model": "m", "system_prompt": "p",
        "skills": [], "hooks": [],
    })
    client.post("/api/v1/workflows", json={"id": "wf-three-step"})
    client.post(
        "/api/v1/workflows/wf-three-step/versions",
        json={"steps": [
            {"name": "first", "role_id": "role-r1"},
            {"name": "second", "role_id": "role-r2"},
            {"name": "third", "role_id": "role-r3"},
        ]},
    )
    plan = client.post("/api/v1/plans", json={
        "repo": "three-step/repo", "intent": "do three",
    }).json()
    task = client.post("/api/v1/tasks", json={
        "plan_id": plan["id"], "title": "TTT", "workflow": "wf-three-step",
    }).json()
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT s.id, s.step_index FROM workflow_run_steps s "
                "JOIN workflow_runs r ON r.id = s.run_id "
                "WHERE r.task_id = :t ORDER BY s.step_index"
            ),
            {"t": task["id"]},
        ).all()
    step1_id = str(rows[0].id)
    step2_id = str(rows[1].id)
    step3_id = str(rows[2].id)

    # Complete step 2 BEFORE step 1 (insertion order is not contract).
    with engine.begin() as conn:
        for sid, decision in ((step2_id, "second-done"), (step1_id, "first-done")):
            conn.execute(
                sa.text(
                    "UPDATE workflow_run_steps "
                    "SET status='completed', output=CAST(:o AS jsonb), "
                    "    completed_at=now() "
                    "WHERE id=:s"
                ),
                {
                    "o": __import__("json").dumps({"decision": decision}),
                    "s": sid,
                },
            )

    resp = client.get(f"/api/v1/steps/{step3_id}")
    assert resp.status_code == 200, resp.text
    prior = resp.json()["prior_steps"]
    assert [p["step_index"] for p in prior] == [0, 1]
    assert prior[0]["output"]["decision"] == "first-done"
    assert prior[1]["output"]["decision"] == "second-done"


def test_get_step_returns_skills_and_hooks_in_order(
    client: httpx.Client, truncate: None,
) -> None:
    """The role join must return skills + hooks in their declared position
    order — the worker treats the lists as ordered (e.g. system prompts
    are concatenated in skill order, hooks fire in registered order)."""
    for slug in ("s1", "s2", "s3"):
        client.post("/api/v1/skills", json={
            "id": slug, "name": slug, "content": f"content-{slug}",
        })
    client.post("/api/v1/roles", json={
        "id": "r-ordered", "model": "claude", "system_prompt": "p",
        "skills": ["s2", "s1", "s3"],  # intentionally out of alphabetical order
    })
    client.post("/api/v1/workflows", json={"id": "wf-ordered"})
    client.post(
        "/api/v1/workflows/wf-ordered/versions",
        json={"steps": [{"name": "step0", "role_id": "r-ordered"}]},
    )

    plan = client.post("/api/v1/plans", json={
        "repo": "ord/repo", "intent": "x",
    }).json()
    task = client.post("/api/v1/tasks", json={
        "plan_id": plan["id"], "title": "T", "workflow": "wf-ordered",
    }).json()
    step_id = _await_step(client, task["id"])

    resp = client.get(f"/api/v1/steps/{step_id}")
    assert resp.status_code == 200, resp.text
    skill_ids = [s["id"] for s in resp.json()["role"]["skills"]]
    assert skill_ids == ["s2", "s1", "s3"]
