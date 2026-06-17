"""Unit tests for ``GET /api/v1/dashboard/tasks/{task_id}`` (ADR-0056, PR-B2).

Exercises the route handler directly with a stub async session — no
live database. The stub dispatches by SQL substring to fixture-driven
row lists, mirroring ``test_routers_dashboard_overview.py``.

Coverage:

  * Happy path — task with 2+ runs, each with steps. Payload shape
    matches ``services/dashboard/src/api/types.ts`` ``TaskDetail``
    (``task`` is the same shape as in ``/overview``'s ``tasks``
    array; ``runs`` is a flat ``Run[]`` with each run carrying its
    own ``steps``).
  * 404 — unknown task_id returns ``404``.
  * No runs — a registered task with zero ``workflow_runs`` returns
    ``runs: []`` (registered/queued tasks legitimately have no runs).
  * Failing latest run — the per-run status derivation rolls the
    ``failed`` step up to ``status: 'failed'``, and the task's
    ``derived_status`` / PR fields propagate to the ``task`` block.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.dashboard import router as dashboard_router


# ── Stub session ──────────────────────────────────────────────────────────────


class _StubResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_StubResult":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def one_or_none(self) -> dict[str, Any] | None:
        if not self._rows:
            return None
        return self._rows[0]


class _StubSession:
    """Routes ``session.execute(text(SQL), params)`` to fixtures by SQL
    substring. The task-detail handler issues 4 queries; we key on a
    unique fragment of each."""

    def __init__(
        self,
        *,
        task: dict[str, Any] | None = None,
        escalation: dict[str, Any] | None = None,
        pipeline: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        run_steps: list[dict[str, Any]] | None = None,
    ) -> None:
        self.task = task
        self.escalation = escalation
        self.pipeline = pipeline or []
        self.runs = runs or []
        self.run_steps = run_steps or []
        self.recorded_params: list[dict[str, Any] | None] = []

    async def execute(
        self, statement: Any, params: dict[str, Any] | None = None,
    ) -> _StubResult:
        self.recorded_params.append(params)
        sql = statement.text if hasattr(statement, "text") else str(statement)

        if "FROM tasks t" in sql and "WHERE t.id = :task_id" in sql:
            return _StubResult([self.task] if self.task else [])
        if "last_escalation" in sql:
            return _StubResult([self.escalation] if self.escalation else [])
        # ADR-0087: task_executions replaced workflow_runs/_steps; the
        # three queries keep their distinguishing params (:run_id single,
        # :task_id history, :run_ids bulk).
        if "FROM task_executions te" in sql and ":run_id" in sql and "ANY" not in sql:
            return _StubResult(self.pipeline)
        if "FROM task_executions te" in sql and "WHERE te.task_id = :task_id" in sql:
            return _StubResult(self.runs)
        if "FROM task_executions te" in sql and ":run_ids" in sql:
            return _StubResult(self.run_steps)
        raise AssertionError(f"unexpected SQL passed to stub session:\n{sql}")


def _build_app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)

    def _override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    return app


# ── Fixture builders ──────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task_row(
    *,
    task_id: uuid.UUID,
    derived_status: str = "wf-quick: executing",
    repo: str = "zephyr/web",
    account: str | None = "zephyr",
    title: str = "Sample task",
    latest_run_id: uuid.UUID | None = None,
    latest_workflow_id: str | None = "wf-quick",
    pr_number: int | None = None,
    pr_derived_mergeability: str | None = None,
    pr_ci_conclusion: str | None = None,
    tokens_total: int = 1000,
) -> dict[str, Any]:
    now = _now()
    return {
        "id": str(task_id),
        "title": title,
        "repo": repo,
        "plan_id": str(uuid.uuid4()),
        "created_at": now - timedelta(hours=1),
        "derived_status": derived_status,
        "repo_mode": "conform",
        "claude_account": account,
        "pr_number": pr_number,
        "pr_branch": "claude/feature" if pr_number else None,
        "pr_head_sha": "deadbee1234" if pr_number else None,
        "pr_ci_conclusion": pr_ci_conclusion,
        "pr_review_decision": None,
        "pr_validate_decision": None,
        "pr_conflicting": False if pr_number else None,
        "pr_derived_mergeability": pr_derived_mergeability,
        "latest_run_id": latest_run_id,
        "latest_workflow_id": latest_workflow_id,
        "latest_run_started_at": now - timedelta(minutes=30) if latest_run_id else None,
        "last_activity": now - timedelta(minutes=5),
        "tokens_total": tokens_total,
    }


def _run_row(
    *,
    run_id: uuid.UUID,
    workflow_id: str = "wf-quick",
    created_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": str(run_id),
        "workflow_id": workflow_id,
        "created_at": created_at or _now() - timedelta(minutes=30),
    }


def _step_row(
    *,
    run_id: uuid.UUID,
    role_id: str,
    status: str,
    step_index: int,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    output: dict[str, Any] | None = None,
    error: str | None = None,
    input_tokens: int | None = 1000,
    output_tokens: int | None = 500,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "run_id": str(run_id),
        "role_id": role_id,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "output": output,
        "error": error,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "step_index": step_index,
    }


# ── Happy path ────────────────────────────────────────────────────────────────


def test_task_detail_happy_path_two_runs_each_with_steps() -> None:
    """Payload carries the task block + a chronological ``runs`` list,
    each run with its own ``steps``. Shapes match
    ``services/dashboard/src/api/types.ts`` ``TaskDetail`` /
    ``Run`` / ``RunStep`` field-for-field."""
    task_id = uuid.uuid4()
    run_a_id = uuid.uuid4()
    run_b_id = uuid.uuid4()
    now = _now()

    task = _task_row(
        task_id=task_id,
        derived_status="awaiting_review",
        latest_run_id=run_b_id,
        latest_workflow_id="wf-review",
        pr_number=982,
        pr_derived_mergeability="blocked-on-review",
        pr_ci_conclusion="success",
    )

    pipeline = [
        {"role": "plan", "status": "completed", "step_index": 0},
        {"role": "code", "status": "completed", "step_index": 1},
        {"role": "review", "status": "running", "step_index": 2},
    ]

    runs = [
        _run_row(
            run_id=run_a_id, workflow_id="wf-quick",
            created_at=now - timedelta(minutes=60),
        ),
        _run_row(
            run_id=run_b_id, workflow_id="wf-review",
            created_at=now - timedelta(minutes=30),
        ),
    ]

    run_steps = [
        _step_row(
            run_id=run_a_id, role_id="plan", status="completed", step_index=0,
            started_at=now - timedelta(minutes=60),
            completed_at=now - timedelta(minutes=58),
            output={"summary": "scoped 3 callback sites"},
            input_tokens=4280, output_tokens=1120,
        ),
        _step_row(
            run_id=run_a_id, role_id="code", status="completed", step_index=1,
            started_at=now - timedelta(minutes=58),
            completed_at=now - timedelta(minutes=52),
            output={"summary": "412 LOC", "commit_sha": "d12fab9"},
            input_tokens=18900, output_tokens=6720,
        ),
        _step_row(
            run_id=run_b_id, role_id="review", status="running", step_index=0,
            started_at=now - timedelta(minutes=30),
            completed_at=None,
            output=None,
            input_tokens=0, output_tokens=0,
        ),
    ]

    session = _StubSession(
        task=task, pipeline=pipeline, runs=runs, run_steps=run_steps,
    )
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get(f"/api/v1/dashboard/tasks/{task_id}")

    assert response.status_code == 200, response.text
    body = response.json()

    # Top-level keys mirror the ``TaskDetail`` TS interface exactly.
    assert set(body) == {"task", "runs"}

    # Task block carries the same shape as overview's `tasks` array.
    t = body["task"]
    assert t["id"] == str(task_id)
    assert t["derived_status"] == "awaiting_review"
    assert t["pr"]["pr_number"] == 982
    assert t["pr"]["head_sha"] == "deadbee"  # truncated to 7 chars
    assert t["pr"]["derived_mergeability"] == "blocked-on-review"
    assert t["pipeline"] == [
        {"role": "plan", "status": "done"},
        {"role": "code", "status": "done"},
        {"role": "review", "status": "running"},
    ]
    assert t["workflow"] == "wf-review"
    assert t["escalated"] is False
    assert t["tokens"] == 1000  # from tokens_total

    # Runs — chronological, each with its own steps.
    assert len(body["runs"]) == 2
    run_a, run_b = body["runs"]

    assert run_a["id"] == str(run_a_id)
    assert run_a["workflow_id"] == "wf-quick"
    assert run_a["status"] == "completed"  # both steps completed
    assert run_a["duration_s"] is not None and run_a["duration_s"] > 0
    assert len(run_a["steps"]) == 2
    assert run_a["steps"][0]["role_id"] == "plan"
    assert run_a["steps"][0]["status"] == "completed"
    assert run_a["steps"][0]["output"] == {
        "summary": "scoped 3 callback sites",
        "decision": None,
        "commit_sha": None,
    }
    assert run_a["steps"][0]["tokens"] == {"in": 4280, "out": 1120}
    assert run_a["steps"][1]["output"]["commit_sha"] == "d12fab9"

    assert run_b["id"] == str(run_b_id)
    assert run_b["workflow_id"] == "wf-review"
    assert run_b["status"] == "running"  # one running step
    assert run_b["completed_at"] is None  # not done yet
    assert run_b["duration_s"] is None
    assert run_b["steps"][0]["status"] == "running"


# ── 404 ───────────────────────────────────────────────────────────────────────


def test_task_detail_unknown_task_id_returns_404() -> None:
    session = _StubSession(task=None)
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/dashboard/tasks/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


# ── No runs ───────────────────────────────────────────────────────────────────


def test_task_detail_registered_task_with_no_runs_returns_empty_runs() -> None:
    """A freshly-registered task with no ``workflow_runs`` rows returns
    ``runs: []`` — the endpoint must not return 404 just because the
    scheduler hasn't dispatched yet."""
    task_id = uuid.uuid4()
    task = _task_row(
        task_id=task_id,
        derived_status="registered",
        latest_run_id=None,
        latest_workflow_id=None,
        tokens_total=0,
    )
    session = _StubSession(task=task, runs=[], run_steps=[])
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/dashboard/tasks/{task_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"]["derived_status"] == "registered"
    assert body["task"]["pipeline"] == []  # no latest_run → empty strip
    assert body["task"]["workflow"] is None
    assert body["runs"] == []


# ── Failing latest run ────────────────────────────────────────────────────────


def test_task_detail_failing_latest_run_propagates_status_fields() -> None:
    """A run with a ``failed`` step rolls up to ``run.status == 'failed'``,
    and the task block reflects the upstream blocked PR / escalation
    state."""
    task_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = _now()

    task = _task_row(
        task_id=task_id,
        derived_status="blocked-on-ci",
        latest_run_id=run_id,
        latest_workflow_id="wf-ci-fix",
        pr_number=4128,
        pr_derived_mergeability="blocked-on-ci",
        pr_ci_conclusion="failure",
    )
    escalation = {
        "escalated_at": now - timedelta(minutes=10),
        "reason": "stuck > 10m on failing CI: e2e-auth",
    }
    pipeline = [
        {"role": "ci-analyze", "status": "completed", "step_index": 0},
        {"role": "code", "status": "failed", "step_index": 1},
    ]
    runs = [
        _run_row(
            run_id=run_id, workflow_id="wf-ci-fix",
            created_at=now - timedelta(minutes=14),
        ),
    ]
    run_steps = [
        _step_row(
            run_id=run_id, role_id="ci-analyze", status="completed", step_index=0,
            started_at=now - timedelta(minutes=14),
            completed_at=now - timedelta(minutes=13),
            output={"summary": "e2e-auth timing out"},
            input_tokens=3900, output_tokens=880,
        ),
        _step_row(
            run_id=run_id, role_id="code", status="failed", step_index=1,
            started_at=now - timedelta(minutes=13),
            completed_at=now - timedelta(minutes=12),
            output=None,
            error="Patch reverted by review-bot",
            input_tokens=6120, output_tokens=2010,
        ),
    ]

    session = _StubSession(
        task=task, escalation=escalation,
        pipeline=pipeline, runs=runs, run_steps=run_steps,
    )
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/dashboard/tasks/{task_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    # Task block carries the blocked PR + escalation state.
    t = body["task"]
    assert t["derived_status"] == "blocked-on-ci"
    assert t["escalated"] is True
    assert t["escalation_reason"] == "stuck > 10m on failing CI: e2e-auth"
    assert t["pr"]["ci_conclusion"] == "failure"
    assert t["pr"]["derived_mergeability"] == "blocked-on-ci"

    # Run rolls up to failed even though the first step completed.
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["status"] == "failed"
    assert run["completed_at"] is not None  # terminal status → completion timestamp populated
    assert run["duration_s"] is not None
    assert [s["status"] for s in run["steps"]] == ["completed", "failed"]
    assert run["steps"][1]["error"] == "Patch reverted by review-bot"
    assert run["steps"][1]["output"] is None


# ── Auto-discovery sanity ─────────────────────────────────────────────────────


def test_task_detail_router_is_auto_discovered() -> None:
    """``task_detail.py`` exports a module-level ``router`` — the
    package's ``_discover_and_mount`` walk picks it up without any
    ``__init__.py`` edit. This is the contract PR-B2..B5 rely on; if
    this regresses, every dashboard PR after B1 starts conflicting."""
    from treadmill_api.routers import dashboard as dashboard_pkg

    assert "task_detail" in dashboard_pkg.MOUNTED_MODULES
    paths = {getattr(r, "path", None) for r in dashboard_pkg.router.routes}
    assert "/api/v1/dashboard/tasks/{task_id}" in paths
