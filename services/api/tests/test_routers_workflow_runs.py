"""Unit tests for ``/api/v1/workflow_runs`` and ``/api/v1/workflow_run_steps``
(Task B of the combined ADR-0085+0086 plan) plus ``/api/v1/task_prs``
(Task B amendment).

The routers exercise SQLAlchemy session.get / execute / flush / commit /
refresh; the stub session below mocks those seams without touching
Postgres so this suite runs in the default unit pass.

Coverage axes
=============

* POST /api/v1/workflow_runs:
    - creates run + author step, returns both IDs
    - 404 on unknown task_id
    - 409 on duplicate run for the same task_id
* PATCH /api/v1/workflow_run_steps/{step_id}:
    - status=running sets started_at
    - status=completed sets completed_at
    - 404 on unknown step_id
* POST /api/v1/task_prs:
    - creates a row + 201 + body
    - 409 on duplicate (repo, pr_number)
    - 404 on unknown task_id
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Task, TaskPR, WorkflowRun, WorkflowRunStep
from treadmill_api.routers.task_prs import router as task_prs_router
from treadmill_api.routers.workflow_runs import router as workflow_runs_router


# ── Stub data ─────────────────────────────────────────────────────────────


class _StubTask:
    """Stand-in for a Task row (only the fields the router reads)."""

    def __init__(
        self, *, task_id: uuid.UUID, workflow_version_id: uuid.UUID
    ) -> None:
        self.id = task_id
        self.workflow_version_id = workflow_version_id


class _StubRun:
    def __init__(
        self,
        *,
        task_id: uuid.UUID,
        workflow_version_id: uuid.UUID,
        trigger: str,
    ) -> None:
        self.id = uuid.uuid4()
        self.task_id = task_id
        self.workflow_version_id = workflow_version_id
        self.trigger = trigger
        self.created_at = datetime.now(timezone.utc)


class _StubStep:
    def __init__(
        self,
        *,
        run_id: uuid.UUID,
        step_index: int,
        step_name: str,
        role_id: str,
        status: str,
    ) -> None:
        self.id = uuid.uuid4()
        self.run_id = run_id
        self.step_index = step_index
        self.step_name = step_name
        self.role_id = role_id
        self.status = status
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None


class _StubTaskPR:
    def __init__(
        self,
        *,
        repo: str,
        pr_number: int,
        task_id: uuid.UUID,
        branch: str | None,
    ) -> None:
        self.repo = repo
        self.pr_number = pr_number
        self.task_id = task_id
        self.branch = branch
        self.created_at = datetime.now(timezone.utc)
        self.closed_at: datetime | None = None


# ── Stub session ──────────────────────────────────────────────────────────


class _StubSession:
    """In-memory session sufficient for these routers."""

    def __init__(self) -> None:
        self._tasks: dict[uuid.UUID, _StubTask] = {}
        self._runs: dict[uuid.UUID, _StubRun] = {}
        self._steps: dict[uuid.UUID, _StubStep] = {}
        self._task_prs: dict[tuple[str, int], _StubTaskPR] = {}
        self._pending_add: Any | None = None
        self.commit_calls = 0

    # ── Seeding helpers ──────────────────────────────────────────
    def seed_task(self, *, workflow_version_id: uuid.UUID) -> uuid.UUID:
        task_id = uuid.uuid4()
        self._tasks[task_id] = _StubTask(
            task_id=task_id, workflow_version_id=workflow_version_id
        )
        return task_id

    def seed_run(
        self, *, task_id: uuid.UUID, workflow_version_id: uuid.UUID
    ) -> _StubRun:
        run = _StubRun(
            task_id=task_id,
            workflow_version_id=workflow_version_id,
            trigger="coordinator",
        )
        self._runs[run.id] = run
        return run

    def seed_step(self, *, run_id: uuid.UUID, status: str = "pending") -> _StubStep:
        step = _StubStep(
            run_id=run_id,
            step_index=0,
            step_name="author",
            role_id="role-code-author",
            status=status,
        )
        self._steps[step.id] = step
        return step

    def seed_task_pr(
        self, *, repo: str, pr_number: int, task_id: uuid.UUID
    ) -> _StubTaskPR:
        pr = _StubTaskPR(
            repo=repo, pr_number=pr_number, task_id=task_id, branch="legacy"
        )
        self._task_prs[(repo, pr_number)] = pr
        return pr

    # ── AsyncSession surface ─────────────────────────────────────
    async def get(self, model: type, pk: Any) -> Any:
        if model is Task:
            return self._tasks.get(pk)
        if model is WorkflowRunStep:
            return self._steps.get(pk)
        if model is WorkflowRun:
            return self._runs.get(pk)
        if model is TaskPR:
            return self._task_prs.get(pk)
        return None

    async def execute(self, stmt: Any) -> "_StubResult":
        compiled = str(stmt)
        # SELECT WorkflowRun.id WHERE task_id = :task_id
        if "FROM workflow_runs" in compiled:
            target_task_id = _extract_param(stmt, "task_id")
            for run in self._runs.values():
                if run.task_id == target_task_id:
                    return _StubResult(run.id)
            return _StubResult(None)
        # SELECT TaskPR WHERE repo = ... AND pr_number = ...
        if "FROM task_prs" in compiled:
            repo = _extract_param(stmt, "repo")
            pr_number = _extract_param(stmt, "pr_number")
            return _StubResult(self._task_prs.get((repo, pr_number)))
        return _StubResult(None)

    def add(self, row: Any) -> None:
        # Track the most recently added row so flush() can assign an id
        # if the test mode expects it. For TaskPR + WorkflowRun + Step
        # the stubs already have ids assigned at construction time.
        if isinstance(row, _StubRun):
            self._runs[row.id] = row
        elif isinstance(row, _StubStep):
            self._steps[row.id] = row
        elif isinstance(row, _StubTaskPR):
            self._task_prs[(row.repo, row.pr_number)] = row
        elif isinstance(row, WorkflowRun):
            # Real model instances coming through the router — wrap as
            # stubs so the stub session's lookup helpers stay
            # type-consistent. The real WorkflowRun.id has a
            # server_default; assign locally so the test path doesn't
            # depend on Postgres round-tripping.
            row.id = uuid.uuid4()
            stub = _StubRun(
                task_id=row.task_id,
                workflow_version_id=row.workflow_version_id,
                trigger=row.trigger,
            )
            stub.id = row.id
            self._runs[stub.id] = stub
        elif isinstance(row, WorkflowRunStep):
            row.id = uuid.uuid4()
            stub = _StubStep(
                run_id=row.run_id,
                step_index=row.step_index,
                step_name=row.step_name,
                role_id=row.role_id,
                status=row.status,
            )
            stub.id = row.id
            self._steps[stub.id] = stub
        elif isinstance(row, TaskPR):
            # No server-default to populate; row already has
            # composite key from request.
            row.created_at = datetime.now(timezone.utc)
            stub = _StubTaskPR(
                repo=row.repo,
                pr_number=row.pr_number,
                task_id=row.task_id,
                branch=row.branch,
            )
            stub.created_at = row.created_at
            self._task_prs[(stub.repo, stub.pr_number)] = stub

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commit_calls += 1

    async def refresh(self, row: Any) -> None:
        return None


class _StubResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


def _extract_param(stmt: Any, name: str) -> Any:
    """Pull a bound parameter value out of a compiled select.

    SQLAlchemy 2.x ORM SELECT statements generate suffixed param names
    (``task_id_1``, ``repo_1``, ``pr_number_1``) rather than the bare
    column name, so match by ``startswith(name)``.
    """
    try:
        params = stmt.compile().params
        for key, value in params.items():
            if key == name or key.startswith(f"{name}_"):
                return value
        return None
    except Exception:
        return None


# ── Test app factory ──────────────────────────────────────────────────────


@pytest.fixture
def app_and_session() -> tuple[FastAPI, _StubSession]:
    """Return a FastAPI app wired to both routers + a session override."""
    app = FastAPI()
    app.include_router(workflow_runs_router)
    app.include_router(task_prs_router)

    session = _StubSession()

    async def _override_session():
        yield session

    app.dependency_overrides[get_session] = _override_session
    return app, session


# ── POST /api/v1/workflow_runs ────────────────────────────────────────────


class TestCreateWorkflowRun:
    def test_creates_run_and_step(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        app, session = app_and_session
        task_id = session.seed_task(workflow_version_id=uuid.uuid4())
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/workflow_runs",
                json={"task_id": str(task_id), "trigger": "coordinator"},
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert uuid.UUID(body["run_id"])
        assert uuid.UUID(body["step_id"])
        # The step row landed in the stub keyed by the returned step_id.
        step = session._steps[uuid.UUID(body["step_id"])]
        assert step.step_name == "author"
        assert step.role_id == "role-code-author"
        assert step.status == "pending"
        assert session.commit_calls == 1

    def test_404_on_unknown_task(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        app, _ = app_and_session
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/workflow_runs",
                json={"task_id": str(uuid.uuid4()), "trigger": "coordinator"},
            )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    def test_409_on_duplicate_run_for_task(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        app, session = app_and_session
        wv_id = uuid.uuid4()
        task_id = session.seed_task(workflow_version_id=wv_id)
        # Pre-seed an existing run for this task.
        session.seed_run(task_id=task_id, workflow_version_id=wv_id)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/workflow_runs",
                json={"task_id": str(task_id), "trigger": "coordinator"},
            )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]


# ── PATCH /api/v1/workflow_run_steps/{step_id} ────────────────────────────


class TestUpdateWorkflowRunStep:
    def test_patch_status_running_sets_started_at(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        app, session = app_and_session
        run_id = session.seed_run(
            task_id=uuid.uuid4(), workflow_version_id=uuid.uuid4()
        ).id
        step = session.seed_step(run_id=run_id, status="pending")
        started_at = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/v1/workflow_run_steps/{step.id}",
                json={"status": "running", "started_at": started_at.isoformat()},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "running"
        assert body["started_at"] is not None
        assert body["completed_at"] is None
        assert step.status == "running"
        assert step.started_at == started_at

    def test_patch_status_completed_sets_completed_at(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        app, session = app_and_session
        run_id = session.seed_run(
            task_id=uuid.uuid4(), workflow_version_id=uuid.uuid4()
        ).id
        step = session.seed_step(run_id=run_id, status="running")
        completed_at = datetime(2026, 6, 9, 12, 30, 0, tzinfo=timezone.utc)
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/v1/workflow_run_steps/{step.id}",
                json={
                    "status": "completed",
                    "completed_at": completed_at.isoformat(),
                },
            )
        assert resp.status_code == 200
        assert step.status == "completed"
        assert step.completed_at == completed_at

    def test_patch_404_for_unknown_step(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        app, _ = app_and_session
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/v1/workflow_run_steps/{uuid.uuid4()}",
                json={"status": "running"},
            )
        assert resp.status_code == 404

    def test_patch_partial_update_only_writes_provided_fields(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        """Omitted fields stay at their prior value — model_fields_set
        distinguishes omitted from null."""
        app, session = app_and_session
        run_id = session.seed_run(
            task_id=uuid.uuid4(), workflow_version_id=uuid.uuid4()
        ).id
        step = session.seed_step(run_id=run_id, status="running")
        original_started = datetime(2026, 6, 9, 11, 0, 0, tzinfo=timezone.utc)
        step.started_at = original_started

        with TestClient(app) as client:
            # Only status; do NOT include started_at — it must stay.
            resp = client.patch(
                f"/api/v1/workflow_run_steps/{step.id}",
                json={"status": "completed"},
            )
        assert resp.status_code == 200
        assert step.status == "completed"
        assert step.started_at == original_started


# ── POST /api/v1/task_prs (Task B amendment) ──────────────────────────────


class TestCreateTaskPR:
    def test_creates_row_and_returns_201(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        app, session = app_and_session
        task_id = session.seed_task(workflow_version_id=uuid.uuid4())
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/task_prs",
                json={
                    "repo": "joeLepper/treadmill",
                    "pr_number": 271,
                    "task_id": str(task_id),
                    "branch": "bert/task-status-pr-merged-precedence",
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["repo"] == "joeLepper/treadmill"
        assert body["pr_number"] == 271
        assert body["branch"] == "bert/task-status-pr-merged-precedence"
        assert ("joeLepper/treadmill", 271) in session._task_prs

    def test_409_on_duplicate_repo_and_pr_number(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        app, session = app_and_session
        task_id = session.seed_task(workflow_version_id=uuid.uuid4())
        session.seed_task_pr(
            repo="joeLepper/treadmill", pr_number=42, task_id=task_id
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/task_prs",
                json={
                    "repo": "joeLepper/treadmill",
                    "pr_number": 42,
                    "task_id": str(task_id),
                    "branch": "any",
                },
            )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    def test_404_on_unknown_task(
        self, app_and_session: tuple[FastAPI, _StubSession]
    ) -> None:
        app, _ = app_and_session
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/task_prs",
                json={
                    "repo": "joeLepper/treadmill",
                    "pr_number": 999,
                    "task_id": str(uuid.uuid4()),
                    "branch": "x",
                },
            )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]
