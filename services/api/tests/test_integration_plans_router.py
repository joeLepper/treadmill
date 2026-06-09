"""Integration tests for the Plans router against live Postgres + API.

These tests issue real HTTP requests to the API container running in the
spike substrate. Skipped by default; opt in with ``TREADMILL_INTEGRATION=1``.
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
    """Wait for the API + a Plans endpoint to be reachable."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                # The Plans router won't 404 on /api/v1/plans/<missing-id>
                # if it's wired; we'll do the actual check inside tests.
                break
        except Exception:
            time.sleep(0.5)
    with httpx.Client(base_url=api_url, timeout=10.0) as c:
        yield c


# Truncate before each test for clean state. We can't reuse the same fixture
# from test_integration_task_status.py without pulling its TableTruncator;
# inline a simpler version here.
_TEST_TABLES = (
    "plans",
    "tasks",
    "task_prs",
    "task_dependencies",
    "workflow_runs",
    "workflow_run_steps",
    "events",
    "team_configs",
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
    + a role-author role.

    Required because POST /plans with doc_content resolves the
    workflow_version_id from the slug; without a registered workflow the
    request 400s. The version step is required so the dispatcher can
    materialize a WorkflowRunStep when the task is created.
    """
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO workflows (id) VALUES ('wf-author')"))
        wv_id = conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES ('wf-author', 1) RETURNING id"
        )).scalar()
        conn.execute(sa.text(
            "INSERT INTO roles (id, model, system_prompt, output_kind) "
            "VALUES ('role-author', 'claude', '', 'code')"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_version_steps "
            "(workflow_version_id, step_index, step_name, role_id) "
            "VALUES (:wv, 0, 'author', 'role-author')"
        ), {"wv": wv_id})
    yield


# ── POST /plans (Scenario 2: intent only) ─────────────────────────────────────


def test_create_plan_with_intent_only(client: httpx.Client, truncate: None) -> None:
    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "intent": "Add a billing page",
            "created_by": "test@example.com",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["repo"] == "test/repo"
    assert body["intent"] == "Add a billing page"
    assert body["doc_path"] is None
    assert body["created_by"] == "test@example.com"


def test_create_plan_with_neither_intent_nor_doc_returns_422(client: httpx.Client) -> None:
    response = client.post("/api/v1/plans", json={"repo": "test/repo"})
    assert response.status_code == 422


# ── POST /plans (Scenario 1: with doc_content) ───────────────────────────────


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
  - id: t1
    title: "Second task"
    workflow: wf-author
    depends_on:
      - task.t0.pr_merged
    intent: Second task description
    scope:
      files: [b.py]
    validation:
      - kind: deterministic
        description: "tests still pass"
```
"""


def test_create_plan_with_doc_spawns_tasks(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
) -> None:
    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "doc_path": "docs/plans/2026-05-08-test.md",
            "doc_content": _PLAN_DOC_TEMPLATE,
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["doc_path"] == "docs/plans/2026-05-08-test.md"

    tasks_resp = client.get(f"/api/v1/plans/{plan['id']}/tasks")
    assert tasks_resp.status_code == 200
    tasks = tasks_resp.json()
    assert len(tasks) == 2
    titles = [t["title"] for t in tasks]
    assert "First task" in titles
    assert "Second task" in titles


def test_create_plan_with_doc_reads_auto_merge_false_frontmatter(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
    engine: Engine,
) -> None:
    """ADR-0031 Q31.c per-plan opt-out wires from frontmatter to Plan row."""
    doc = "---\nauto_merge: false\n---\n\n" + _PLAN_DOC_TEMPLATE
    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "doc_path": "docs/plans/opt-out.md",
            "doc_content": doc,
        },
    )
    assert response.status_code == 201, response.text
    plan_id = response.json()["id"]

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT auto_merge FROM plans WHERE id = :id"),
            {"id": plan_id},
        ).scalar_one()
    assert row is False


def test_create_plan_with_doc_no_frontmatter_leaves_auto_merge_null(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
    engine: Engine,
) -> None:
    """No frontmatter → auto_merge stays NULL → enabled by default."""
    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "doc_path": "docs/plans/default.md",
            "doc_content": _PLAN_DOC_TEMPLATE,
        },
    )
    assert response.status_code == 201, response.text
    plan_id = response.json()["id"]

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT auto_merge FROM plans WHERE id = :id"),
            {"id": plan_id},
        ).scalar_one()
    assert row is None


def test_create_plan_with_unknown_workflow_returns_400(
    client: httpx.Client,
    truncate: None,
) -> None:
    """No workflow registered → 400 with a clear message."""
    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "doc_path": "docs/plans/x.md",
            "doc_content": _PLAN_DOC_TEMPLATE,
        },
    )
    assert response.status_code == 400, response.text
    assert "wf-author" in response.json()["detail"]


def test_create_plan_with_malformed_doc_returns_400(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
) -> None:
    """Missing required fields are rejected by the parser; the API
    surfaces the parse error as 400 with detail."""
    bad_doc = """## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "x"
    # missing workflow + intent + scope + validation
```
"""
    response = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "doc_content": bad_doc},
    )
    # 400 from our explicit parse check, OR 422 if Pydantic field-level
    # validation kicks in first. Either way, NOT 201.
    assert response.status_code in (400, 422)


# ── Task D — team_configs auto-routing + plan.submitted event ────────────────


def _seed_team_config(
    engine: Engine,
    *,
    repo: str,
    coordinator_label: str,
    worker_labels: list[str] | None = None,
) -> None:
    """Insert a single ``team_configs`` row + return after commit."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO team_configs "
                "(repo, coordinator_label, worker_labels) "
                "VALUES (:repo, :coord, :workers)"
            ),
            {
                "repo": repo,
                "coord": coordinator_label,
                "workers": worker_labels or [],
            },
        )


def test_create_plan_when_team_config_exists_preserves_created_by_and_emits_submitted(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """ADR-0085+0086 Task D — a repo with a team_configs row:
    (a) created_by is NOT overridden to the coordinator label; it is
    preserved verbatim from the request (or stays None when omitted), and
    (b) emits a plan.submitted event whose payload carries coordinator_label.
    The coordinator discovers plans via coordinator_label in the event
    payload, not via created_by."""
    _seed_team_config(
        engine,
        repo="team-d/auto-routing",
        coordinator_label="coord-team-alpha",
    )

    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "team-d/auto-routing",
            "intent": "test the auto-routing path",
            "created_by": "treadmill-alan",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    plan_id = body["id"]

    # (a) created_by is the submitting orchestrator, not the coordinator.
    with engine.connect() as conn:
        plan_row = conn.execute(
            sa.text("SELECT created_by FROM plans WHERE id = :id"),
            {"id": plan_id},
        ).fetchone()
    assert plan_row is not None
    assert plan_row[0] == "treadmill-alan"

    # (b) the plan.submitted event landed in the events table with the
    # right payload shape.
    with engine.connect() as conn:
        submitted_event = conn.execute(
            sa.text(
                "SELECT payload FROM events "
                "WHERE plan_id = :id AND entity_type = 'plan' "
                "AND action = 'submitted'"
            ),
            {"id": plan_id},
        ).fetchone()
    assert submitted_event is not None
    payload = submitted_event[0]
    assert payload["repo"] == "team-d/auto-routing"
    assert payload["coordinator_label"] == "coord-team-alpha"
    # Scenario 2 intent-only → no tasks spawned.
    assert payload["task_count"] == 0


def test_create_plan_when_no_team_config_preserves_created_by_and_skips_submitted(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """ADR-0085+0086 Task D — a repo with NO team_configs row:
    (a) preserves the request body's created_by verbatim (no auto-
    override), and (b) does NOT emit a plan.submitted event. Repos
    without a team config stay on the legacy lifecycle."""
    # No team_configs row for this repo.
    explicit_created_by = "user-explicit-author"
    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "team-d/no-config",
            "intent": "test the legacy path",
            "created_by": explicit_created_by,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    plan_id = body["id"]

    # (a) created_by lands verbatim.
    with engine.connect() as conn:
        plan_row = conn.execute(
            sa.text("SELECT created_by FROM plans WHERE id = :id"),
            {"id": plan_id},
        ).fetchone()
    assert plan_row is not None
    assert plan_row[0] == explicit_created_by

    # (b) NO plan.submitted event landed.
    with engine.connect() as conn:
        submitted_event = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM events "
                "WHERE plan_id = :id AND action = 'submitted'"
            ),
            {"id": plan_id},
        ).scalar()
    assert submitted_event == 0


def test_create_plan_with_doc_and_team_config_includes_task_count(
    client: httpx.Client, engine: Engine, truncate: None,
    seed_wf_author: None,
) -> None:
    """Scenario 1 + team_config: the plan.submitted event's task_count
    reflects the number of tasks spawned by the doc."""
    _seed_team_config(
        engine,
        repo="team-d/with-tasks",
        coordinator_label="coord-team-beta",
    )

    doc = """\
# Task D Test Plan

## Sequence of work

```yaml
sequence_of_work:
  - id: t1
    title: First task
    workflow: wf-author
    depends_on: []
    intent: do stuff
  - id: t2
    title: Second task
    workflow: wf-author
    depends_on: [t1]
    intent: do more stuff
```
"""

    response = client.post(
        "/api/v1/plans",
        json={
            "repo": "team-d/with-tasks",
            "doc_content": doc,
            "doc_path": "docs/plans/team-d-test.md",
        },
    )
    assert response.status_code == 201, response.text
    plan_id = response.json()["id"]

    with engine.connect() as conn:
        submitted_event = conn.execute(
            sa.text(
                "SELECT payload FROM events "
                "WHERE plan_id = :id AND action = 'submitted'"
            ),
            {"id": plan_id},
        ).fetchone()
    assert submitted_event is not None
    payload = submitted_event[0]
    assert payload["task_count"] == 2
    assert payload["coordinator_label"] == "coord-team-beta"


# ── GET /plans/{id} ───────────────────────────────────────────────────────────


def test_get_plan_returns_created_fields(client: httpx.Client, truncate: None) -> None:
    create = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "intent": "x"},
    )
    plan_id = create.json()["id"]
    fetch = client.get(f"/api/v1/plans/{plan_id}")
    assert fetch.status_code == 200
    body = fetch.json()
    assert body["id"] == plan_id
    assert body["intent"] == "x"


def test_get_plan_returns_404_for_unknown_id(client: httpx.Client) -> None:
    response = client.get(f"/api/v1/plans/{uuid.uuid4()}")
    assert response.status_code == 404


def test_get_plan_returns_422_for_invalid_uuid(client: httpx.Client) -> None:
    response = client.get("/api/v1/plans/not-a-uuid")
    assert response.status_code == 422


# ── GET /plans/{id}/tasks ─────────────────────────────────────────────────────


def test_list_plan_tasks_includes_derived_status(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
) -> None:
    """Per ADR-0010, the dispatcher auto-creates a WorkflowRun for each
    spawned task; the task_status VIEW reports ``<workflow>: executing``
    for tasks whose dependencies are satisfied, and ``blocked`` for tasks
    whose ``depends_on`` is not yet met.

    The default plan-doc template makes ``t1`` depend on ``t0.pr_merged`` —
    so ``t0`` is ``executing`` (no deps) and ``t1`` is ``blocked``.
    """
    create = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "doc_path": "docs/plans/x.md",
            "doc_content": _PLAN_DOC_TEMPLATE,
        },
    )
    plan_id = create.json()["id"]
    tasks_resp = client.get(f"/api/v1/plans/{plan_id}/tasks")
    tasks = tasks_resp.json()
    assert len(tasks) == 2
    by_title = {t["title"]: t for t in tasks}
    assert by_title["First task"]["derived_status"] == "wf-author: executing"
    assert by_title["Second task"]["derived_status"] == "blocked"


def test_list_plan_tasks_returns_empty_for_intent_only_plan(
    client: httpx.Client,
    truncate: None,
) -> None:
    create = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "intent": "x"},
    )
    tasks_resp = client.get(f"/api/v1/plans/{create.json()['id']}/tasks")
    assert tasks_resp.status_code == 200
    assert tasks_resp.json() == []


# ── POST /plans/{id}/submit-doc ───────────────────────────────────────────────


def test_submit_doc_attaches_doc_and_spawns_tasks(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
) -> None:
    create = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "intent": "x"},
    )
    plan_id = create.json()["id"]
    submit = client.post(
        f"/api/v1/plans/{plan_id}/submit-doc",
        json={"doc_path": "docs/plans/x.md", "doc_content": _PLAN_DOC_TEMPLATE},
    )
    assert submit.status_code == 200, submit.text
    assert submit.json()["doc_path"] == "docs/plans/x.md"

    tasks_resp = client.get(f"/api/v1/plans/{plan_id}/tasks")
    assert len(tasks_resp.json()) == 2


def test_submit_doc_twice_returns_409(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
) -> None:
    create = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "intent": "x"},
    )
    plan_id = create.json()["id"]
    first = client.post(
        f"/api/v1/plans/{plan_id}/submit-doc",
        json={"doc_path": "docs/plans/x.md", "doc_content": _PLAN_DOC_TEMPLATE},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/plans/{plan_id}/submit-doc",
        json={"doc_path": "docs/plans/y.md", "doc_content": _PLAN_DOC_TEMPLATE},
    )
    assert second.status_code == 409


# ── A.6 — lifecycle event emission on plan create ─────────────────────────────


def test_create_plan_persists_plan_registered_event(
    client: httpx.Client,
    truncate: None,
    engine: Engine,
) -> None:
    """Scenario 2 (intent only) emits exactly one ``plan.registered`` row.
    No ``plan.activated`` because the plan is still drafting."""
    create = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "intent": "Add a billing page"},
    )
    assert create.status_code == 201
    plan_id = create.json()["id"]
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT entity_type, action, payload FROM events "
                "WHERE plan_id = :id ORDER BY created_at"
            ),
            {"id": plan_id},
        ).all()
    actions = [(r.entity_type, r.action) for r in rows]
    assert ("plan", "registered") in actions
    registered = next(r for r in rows if r.action == "registered")
    assert registered.payload["repo"] == "test/repo"
    assert registered.payload["intent"] == "Add a billing page"


def test_create_plan_scenario_1_emits_plan_activated(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
    engine: Engine,
) -> None:
    """Scenario 1 (with doc_content) emits ``plan.registered`` AND
    ``plan.activated`` in the same transaction (decision #4)."""
    create = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "doc_path": "docs/plans/2026-05-08-test.md",
            "doc_content": _PLAN_DOC_TEMPLATE,
        },
    )
    assert create.status_code == 201
    plan_id = create.json()["id"]
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT action FROM events "
                "WHERE plan_id = :id AND entity_type = 'plan' "
                "ORDER BY created_at"
            ),
            {"id": plan_id},
        ).all()
    actions = [r.action for r in rows]
    assert actions == ["registered", "activated"]


def test_create_plan_scenario_2_does_not_emit_plan_activated(
    client: httpx.Client,
    truncate: None,
    engine: Engine,
) -> None:
    """Scenario 2 stays in ``drafting`` — no ``plan.activated`` until
    the doc is submitted or ``wf-plan`` produces it."""
    create = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "intent": "x"},
    )
    plan_id = create.json()["id"]
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


def test_spawn_tasks_persists_task_registered_per_task(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
    engine: Engine,
) -> None:
    """Scenario 1 spawns N tasks; expect N ``task.registered`` rows."""
    create = client.post(
        "/api/v1/plans",
        json={
            "repo": "test/repo",
            "doc_path": "docs/plans/x.md",
            "doc_content": _PLAN_DOC_TEMPLATE,
        },
    )
    plan_id = create.json()["id"]
    with engine.connect() as conn:
        task_registered_count = conn.execute(
            sa.text(
                "SELECT count(*) FROM events "
                "WHERE plan_id = :id AND entity_type = 'task' "
                "AND action = 'registered'"
            ),
            {"id": plan_id},
        ).scalar()
        task_ids = {
            r.task_id for r in conn.execute(
                sa.text(
                    "SELECT task_id FROM events "
                    "WHERE plan_id = :id AND entity_type = 'task' "
                    "AND action = 'registered'"
                ),
                {"id": plan_id},
            ).all()
        }
    assert task_registered_count == 2
    assert len(task_ids) == 2  # distinct task_id per event


# ── D.1 — task_dependencies persistence ───────────────────────────────────────


_PLAN_DOC_WITH_VALID_DEPS = """## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "First"
    workflow: wf-author
    intent: First
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: tests
  - id: t1
    title: "Second"
    workflow: wf-author
    depends_on:
      - task.t0.pr_merged
    intent: Second
    scope:
      files: [b.py]
    validation:
      - kind: deterministic
        description: tests
```
"""


def test_plan_with_depends_on_persists_task_dependencies(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
    engine: Engine,
) -> None:
    """``t1`` declares one dependency on ``t0``; one row in
    ``task_dependencies`` with the sibling-id substituted to a UUID."""
    create = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "doc_content": _PLAN_DOC_WITH_VALID_DEPS},
    )
    assert create.status_code == 201, create.text
    plan_id = create.json()["id"]
    with engine.connect() as conn:
        # Find t0 and t1 by description (which is the spec.intent in the parser).
        rows = conn.execute(
            sa.text(
                "SELECT t.id, t.description FROM tasks t "
                "WHERE t.plan_id = :p"
            ),
            {"p": plan_id},
        ).all()
        task_by_intent = {r.description: r.id for r in rows}
        t0_id = task_by_intent["First"]
        t1_id = task_by_intent["Second"]
        deps = conn.execute(
            sa.text(
                "SELECT task_id, expression FROM task_dependencies "
                "WHERE task_id = :id"
            ),
            {"id": t1_id},
        ).all()
    assert len(deps) == 1
    assert deps[0].expression == f"task.{t0_id}.pr_merged"


_PLAN_DOC_MALFORMED_DEP = """## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "First"
    workflow: wf-author
    intent: First
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: tests
  - id: t1
    title: "Second"
    workflow: wf-author
    depends_on:
      - task.t0.unknown_predicate
    intent: Second
    scope:
      files: [b.py]
    validation:
      - kind: deterministic
        description: tests
```
"""


def test_plan_with_malformed_depends_on_returns_400(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
) -> None:
    """A depends_on expression outside the v0 grammar 400s with a
    clear detail message."""
    resp = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "doc_content": _PLAN_DOC_MALFORMED_DEP},
    )
    assert resp.status_code == 400, resp.text
    assert "depends_on" in resp.json()["detail"]


_PLAN_DOC_UNKNOWN_SIBLING_DEP = """## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "First"
    workflow: wf-author
    depends_on:
      - task.tX.pr_merged
    intent: First
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: tests
```
"""


def test_plan_with_unknown_sibling_in_depends_on_returns_400(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
) -> None:
    """``task.tX.pr_merged`` is grammatically valid but ``tX`` doesn't
    appear in this plan — 400 with detail."""
    resp = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "doc_content": _PLAN_DOC_UNKNOWN_SIBLING_DEP},
    )
    assert resp.status_code == 400, resp.text
    assert "unknown" in resp.json()["detail"].lower()


# ── D.3 — task_validations persistence ────────────────────────────────────────


_PLAN_DOC_WITH_VALIDATIONS = """## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "First"
    workflow: wf-author
    intent: First
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: pytest tests/test_a.py
      - kind: llm-judge
        description: code is readable
  - id: t1
    title: "Second"
    workflow: wf-author
    intent: Second
    scope:
      files: [b.py]
    validation:
      - kind: deterministic
        description: pytest tests/test_b.py
      - kind: llm-judge
        description: code is readable
```
"""


def test_plan_with_validations_persists_rows(
    client: httpx.Client,
    truncate: None,
    seed_wf_author: None,
    engine: Engine,
) -> None:
    """Two tasks each declaring two validation entries → 4 rows in
    ``task_validations`` with correct task_id / position / kind /
    description."""
    create = client.post(
        "/api/v1/plans",
        json={"repo": "test/repo", "doc_content": _PLAN_DOC_WITH_VALIDATIONS},
    )
    assert create.status_code == 201, create.text
    plan_id = create.json()["id"]
    with engine.connect() as conn:
        all_rows = conn.execute(
            sa.text(
                "SELECT tv.task_id, t.description AS task_desc, "
                "tv.position, tv.kind, tv.description "
                "FROM task_validations tv "
                "JOIN tasks t ON t.id = tv.task_id "
                "WHERE t.plan_id = :p "
                "ORDER BY t.description, tv.position"
            ),
            {"p": plan_id},
        ).all()
    assert len(all_rows) == 4
    # Two rows per task, ordered by position 0 then 1.
    first_task_rows = [r for r in all_rows if r.task_desc == "First"]
    assert len(first_task_rows) == 2
    assert first_task_rows[0].position == 0
    assert first_task_rows[0].kind == "deterministic"
    assert first_task_rows[0].description == "pytest tests/test_a.py"
    assert first_task_rows[1].position == 1
    assert first_task_rows[1].kind == "llm-judge"
    assert first_task_rows[1].description == "code is readable"
