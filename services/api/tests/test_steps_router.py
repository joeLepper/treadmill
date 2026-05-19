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

from treadmill_api.routers.steps import (
    SourceStepBlock,
    WorkerContextResponse,
    _RoleBlock,
)


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


def test_worker_context_response_includes_source_step() -> None:
    """Per ADR-0048 + the architect-source-step plumbing: when a run
    was dispatched as a side-effect of an upstream step's completion
    (today: architect ``amend`` → wf-feedback), the response carries
    a ``source_step`` block so the downstream worker reads the
    upstream directive on its initial context fetch.

    ``None`` default — the (vast majority of) dispatch paths that
    don't plumb cross-run context leave the field unset.
    """
    fields = WorkerContextResponse.model_fields
    assert "source_step" in fields, (
        "ADR-0048 plumbing requires the steps router response to "
        "carry ``source_step``; the wf-feedback analyzer reads the "
        "architect's remediation_summary here"
    )
    assert fields["source_step"].default is None


def test_source_step_block_shape() -> None:
    """The block exposes the upstream step's identifying triple +
    its (JSONB) output payload. ``workflow_id`` lets the worker's
    prompt composer dispatch on the upstream workflow shape (e.g.
    treat ``wf-architecture-resolve`` output as an architect
    remediation directive). ``output`` is the raw JSONB envelope from
    ``workflow_run_steps.output`` per ADR-0011."""
    fields = SourceStepBlock.model_fields
    for required in ("step_id", "run_id", "workflow_id", "step_name", "output"):
        assert required in fields, (
            f"SourceStepBlock missing required field {required!r}"
        )


def test_source_step_is_distinct_from_prior_steps() -> None:
    """``source_step`` is the cross-run / cross-workflow plumbing for
    self-triggered dispatches; ``prior_steps`` is the intra-run
    analyzer→action communication path per ADR-0015. Folding them
    together would lose the routing context. Pin the two fields exist
    side-by-side so a future refactor can't quietly collapse them.
    """
    fields = WorkerContextResponse.model_fields
    assert "source_step" in fields
    assert "prior_steps" in fields
    # ``source_step`` is a single nullable block (cross-run, at most
    # one upstream); ``prior_steps`` is a list (intra-run, N steps
    # before the current one).
    assert fields["source_step"].default is None


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


@_integration
def test_get_step_context_populates_source_step_when_set(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """When ``run.source_step_id`` points at an upstream step (today:
    the architect-amend → wf-feedback dispatch wires this), the steps
    router surfaces the upstream step's identifying triple + its
    JSONB ``output`` as a ``source_step`` block on the response. The
    downstream worker reads the architect's ``remediation_summary``
    from ``source_step.output['payload']`` here (ADR-0048 plumbing).

    Construction:
      1. Create the architect role + workflow + plan + task; the
         task's first run is the architect's.
      2. UPDATE the architect step row directly with an ``output``
         payload — a representative ``amend`` verdict carrying a
         ``remediation_summary`` (the directive the analyzer must
         honor verbatim).
      3. Create a second task with a downstream wf-feedback workflow.
         UPDATE its run's ``source_step_id`` to point at the
         architect's step.
      4. GET /steps/{downstream_step_id} and assert ``source_step``
         is populated with the architect's row, including the
         remediation payload (JSONB pass-through from
         ``workflow_run_steps.output`` — the one JSONB column the
         architecture commits to per ADR-0011).
    """
    # Architect role + workflow + task (the upstream step).
    client.post("/api/v1/roles", json={
        "id": "role-architect-test", "model": "claude",
        "system_prompt": "be an architect",
        "output_kind": "analysis",
        "skills": [], "hooks": [],
    })
    client.post(
        "/api/v1/workflows", json={"id": "wf-architecture-resolve"},
    )
    client.post(
        "/api/v1/workflows/wf-architecture-resolve/versions",
        json={"steps": [
            {"name": "resolve", "role_id": "role-architect-test"},
        ]},
    )
    plan = client.post("/api/v1/plans", json={
        "repo": "ok/repo", "intent": "fix it",
    }).json()
    architect_task = client.post("/api/v1/tasks", json={
        "plan_id": plan["id"], "title": "Arch",
        "workflow": "wf-architecture-resolve",
    }).json()

    # Downstream wf-feedback workflow + task (the dispatch we're
    # plumbing into).
    client.post("/api/v1/roles", json={
        "id": "role-feedback-analyzer-test", "model": "claude",
        "system_prompt": "be a feedback analyzer",
        "output_kind": "analysis",
        "skills": [], "hooks": [],
    })
    client.post("/api/v1/workflows", json={"id": "wf-feedback-test"})
    client.post(
        "/api/v1/workflows/wf-feedback-test/versions",
        json={"steps": [
            {"name": "analyze", "role_id": "role-feedback-analyzer-test"},
        ]},
    )
    feedback_task = client.post("/api/v1/tasks", json={
        "plan_id": plan["id"], "title": "FB",
        "workflow": "wf-feedback-test",
    }).json()

    # Fetch the architect step + write a representative amend
    # output payload to its ``output`` JSONB column; fetch the
    # feedback step + point its run's ``source_step_id`` at the
    # architect step (mirroring what the amend trigger does at
    # dispatch time).
    remediation = (
        "Edit services/api/treadmill_api/foo.py:42 to add the "
        "None-guard. Failing check: validate-py-types."
    )
    architect_payload = {
        "summary": "amend",
        "decision": "amend",
        "payload": {
            "verdict": "amend",
            "remediation_summary": remediation,
            "reasoning": "Intent is right; code drops None at the door.",
        },
        "metadata": {},
        "artifacts": [],
    }
    with engine.begin() as conn:
        architect_step_id = conn.execute(
            sa.text(
                "SELECT s.id FROM workflow_run_steps s "
                "JOIN workflow_runs r ON r.id = s.run_id "
                "WHERE r.task_id = :t LIMIT 1"
            ),
            {"t": architect_task["id"]},
        ).scalar()
        conn.execute(
            sa.text(
                "UPDATE workflow_run_steps "
                "SET status='completed', output=CAST(:o AS jsonb) "
                "WHERE id = :sid"
            ),
            {
                "o": __import__("json").dumps(architect_payload),
                "sid": architect_step_id,
            },
        )
        feedback_row = conn.execute(
            sa.text(
                "SELECT s.id AS step_id, r.id AS run_id "
                "FROM workflow_run_steps s "
                "JOIN workflow_runs r ON r.id = s.run_id "
                "WHERE r.task_id = :t LIMIT 1"
            ),
            {"t": feedback_task["id"]},
        ).first()
        feedback_step_id = feedback_row.step_id
        feedback_run_id = feedback_row.run_id
        conn.execute(
            sa.text(
                "UPDATE workflow_runs SET source_step_id = :ssid "
                "WHERE id = :rid"
            ),
            {"ssid": architect_step_id, "rid": feedback_run_id},
        )

    resp = client.get(f"/api/v1/steps/{feedback_step_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("source_step") is not None, (
        "source_step must be populated when run.source_step_id is set; "
        "the wf-feedback analyzer reads the architect remediation here"
    )
    src = body["source_step"]
    assert src["step_id"] == str(architect_step_id)
    assert src["workflow_id"] == "wf-architecture-resolve"
    assert src["step_name"] == "resolve"
    # The JSONB ``output`` flows through unchanged — the analyzer reads
    # ``source_step.output['payload']['remediation_summary']`` verbatim.
    assert src["output"]["decision"] == "amend"
    assert (
        src["output"]["payload"]["remediation_summary"] == remediation
    )


@_integration
def test_get_step_context_source_step_none_when_unset(
    client: httpx.Client, engine: Engine, truncate: None,
) -> None:
    """The (vast majority of) runs that don't plumb cross-run context
    (initial dispatch, webhook fan-out, deadlock arbitration) leave
    ``run.source_step_id`` as ``NULL`` — the response must surface
    ``source_step`` as ``None``, not raise or fabricate."""
    client.post("/api/v1/roles", json={
        "id": "role-author-test", "model": "claude",
        "system_prompt": "be a coder",
        "output_kind": "code",
        "skills": [], "hooks": [],
    })
    client.post("/api/v1/workflows", json={"id": "wf-author-test"})
    client.post(
        "/api/v1/workflows/wf-author-test/versions",
        json={"steps": [
            {"name": "author", "role_id": "role-author-test"},
        ]},
    )
    plan = client.post("/api/v1/plans", json={
        "repo": "ok/repo", "intent": "ship it",
    }).json()
    task = client.post("/api/v1/tasks", json={
        "plan_id": plan["id"], "title": "T", "workflow": "wf-author-test",
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
    assert body.get("source_step") is None
