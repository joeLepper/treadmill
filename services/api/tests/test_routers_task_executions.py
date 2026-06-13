"""Unit tests for ``/api/v1/task_executions`` (ADR-0087 PR-C).

All tests use an in-memory stub session — no live Postgres required.

Coverage axes
=============

POST /api/v1/task_executions
  - creates a row, returns 201 + body with correct fields
  - 404 on unknown task_id
  - 409 on duplicate (task_id, trigger, worker_label, started_at) — coordinator-restart guard
  - 422 on out-of-vocabulary trigger value (Pydantic field_validator)

PATCH /api/v1/task_executions/{id}
  - status=completed sets completed_at when present
  - status=failed + failure_reason sets both fields
  - partial update (only status, no completed_at) leaves other fields untouched
  - 404 on unknown execution id
  - 422 on out-of-vocabulary status value

GET /api/v1/task_executions?task_id=<id>[&status=<status>]
  - returns rows ordered by started_at ascending
  - returns empty list when no rows
  - ?status=running accepted (200) and returns only running rows
  - ?status=invalid returns 422

POST /api/v1/task_executions/reconcile-coordinator-restart
  - returns {reconciled: N} from rowcount
  - idempotent (200 on repeated calls)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Task, TaskExecution
from treadmill_api.routers.task_executions import router


# ── Stub helpers ────────────────────────────────────────────────────────


def _stub_task(task_id: uuid.UUID) -> MagicMock:
    t = MagicMock(spec=Task)
    t.id = task_id
    t.workflow_version_id = uuid.uuid4()
    return t


def _stub_execution(
    *,
    task_id: uuid.UUID,
    worker_label: str = "worker-treadmill-1",
    trigger: str = "initial",
    status: str = "running",
    failure_reason: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> MagicMock:
    ex = MagicMock(spec=TaskExecution)
    ex.id = uuid.uuid4()
    ex.task_id = task_id
    ex.worker_label = worker_label
    ex.trigger = trigger
    ex.status = status
    ex.failure_reason = failure_reason
    ex.started_at = started_at or datetime.now(timezone.utc)
    ex.completed_at = completed_at
    return ex


class _StubSession:
    """Minimal async session stub for in-process tests."""

    def __init__(
        self,
        *,
        get_returns: object = None,
        scalars_returns: list | None = None,
        flush_raises: Exception | None = None,
        rowcount: int = 0,
    ) -> None:
        self._get_returns = get_returns
        self._scalars_returns = scalars_returns or []
        self._flush_raises = flush_raises
        self._rowcount = rowcount
        self.added: list[object] = []

    async def get(self, model_class, pk):  # noqa: ANN001
        return self._get_returns

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        if self._flush_raises is not None:
            raise self._flush_raises
        # Simulate server-side assignment of id/started_at.
        for obj in self.added:
            if not hasattr(obj, "id") or obj.id is None:
                object.__setattr__(obj, "id", uuid.uuid4())
            if not hasattr(obj, "started_at") or obj.started_at is None:
                object.__setattr__(obj, "started_at", datetime.now(timezone.utc))
            if not hasattr(obj, "created_at") or obj.created_at is None:
                object.__setattr__(obj, "created_at", datetime.now(timezone.utc))

    async def refresh(self, obj: object) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def execute(self, stmt):  # noqa: ANN001
        result = MagicMock()
        result.scalars.return_value.all.return_value = self._scalars_returns
        result.rowcount = self._rowcount
        return result


# ── Test fixtures ────────────────────────────────────────────────────────


def _app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    return app


# ── POST tests ───────────────────────────────────────────────────────────


class TestCreateTaskExecution:
    def test_creates_row_returns_201(self) -> None:
        task_id = uuid.uuid4()
        stub_task = _stub_task(task_id)
        session = _StubSession(get_returns=stub_task)
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/task_executions",
            json={
                "task_id": str(task_id),
                "worker_label": "worker-treadmill-1",
                "trigger": "initial",
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["task_id"] == str(task_id)
        assert body["worker_label"] == "worker-treadmill-1"
        assert body["trigger"] == "initial"
        assert body["status"] == "running"
        assert body["failure_reason"] is None
        assert body["completed_at"] is None
        assert "id" in body
        assert len(session.added) == 1

    def test_404_unknown_task_id(self) -> None:
        session = _StubSession(get_returns=None)
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/task_executions",
            json={
                "task_id": str(uuid.uuid4()),
                "worker_label": "worker-x",
                "trigger": "initial",
            },
        )
        assert resp.status_code == 404

    def test_409_duplicate_spawn_on_coordinator_restart(self) -> None:
        """uq_task_executions_spawn fires → 409, not 500.

        Simulates the coordinator-restart race: coordinator re-runs the
        dispatch loop and tries to POST the same (task_id, trigger,
        worker_label, started_at) row it already wrote before crashing.
        The IntegrityError from the DB must surface as 409 so the
        coordinator can short-circuit instead of retrying into an error
        loop.
        """
        task_id = uuid.uuid4()
        integrity_err = IntegrityError(
            "duplicate key value violates unique constraint "
            '"uq_task_executions_spawn"',
            params=None,
            orig=Exception("unique violation"),
        )
        session = _StubSession(
            get_returns=_stub_task(task_id),
            flush_raises=integrity_err,
        )
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/task_executions",
            json={
                "task_id": str(task_id),
                "worker_label": "worker-treadmill-1",
                "trigger": "initial",
            },
        )
        assert resp.status_code == 409, resp.text
        assert "already exists" in resp.json()["detail"]

    def test_422_invalid_trigger(self) -> None:
        task_id = uuid.uuid4()
        session = _StubSession(get_returns=_stub_task(task_id))
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/task_executions",
            json={
                "task_id": str(task_id),
                "worker_label": "worker-x",
                "trigger": "not-a-real-trigger",
            },
        )
        assert resp.status_code == 422

    def test_coordinator_rework_trigger_accepted(self) -> None:
        task_id = uuid.uuid4()
        session = _StubSession(get_returns=_stub_task(task_id))
        client = TestClient(_app(session))

        for trigger in ("coordinator-rework", "evaluator-rework", "peer-review"):
            resp = client.post(
                "/api/v1/task_executions",
                json={
                    "task_id": str(task_id),
                    "worker_label": "worker-x",
                    "trigger": trigger,
                },
            )
            assert resp.status_code == 201, f"failed for trigger={trigger}: {resp.text}"


# ── PATCH tests ──────────────────────────────────────────────────────────


class TestUpdateTaskExecution:
    def test_status_completed_sets_completed_at(self) -> None:
        task_id = uuid.uuid4()
        ex = _stub_execution(task_id=task_id)
        session = _StubSession(get_returns=ex)
        client = TestClient(_app(session))
        completed_ts = "2026-06-10T05:00:00+00:00"

        resp = client.patch(
            f"/api/v1/task_executions/{ex.id}",
            json={"status": "completed", "completed_at": completed_ts},
        )
        assert resp.status_code == 200, resp.text
        assert ex.status == "completed"
        assert ex.completed_at is not None

    def test_status_failed_sets_failure_reason(self) -> None:
        task_id = uuid.uuid4()
        ex = _stub_execution(task_id=task_id)
        session = _StubSession(get_returns=ex)
        client = TestClient(_app(session))

        resp = client.patch(
            f"/api/v1/task_executions/{ex.id}",
            json={"status": "failed", "failure_reason": "coordinator_restart"},
        )
        assert resp.status_code == 200, resp.text
        assert ex.status == "failed"
        assert ex.failure_reason == "coordinator_restart"

    def test_partial_update_only_sets_provided_fields(self) -> None:
        task_id = uuid.uuid4()
        ex = _stub_execution(task_id=task_id, status="running")
        session = _StubSession(get_returns=ex)
        client = TestClient(_app(session))

        resp = client.patch(
            f"/api/v1/task_executions/{ex.id}",
            json={"status": "completed"},
        )
        assert resp.status_code == 200, resp.text
        assert ex.status == "completed"
        assert ex.completed_at is None
        assert ex.failure_reason is None

    def test_404_unknown_execution_id(self) -> None:
        session = _StubSession(get_returns=None)
        client = TestClient(_app(session))

        resp = client.patch(
            f"/api/v1/task_executions/{uuid.uuid4()}",
            json={"status": "completed"},
        )
        assert resp.status_code == 404

    def test_422_invalid_status(self) -> None:
        task_id = uuid.uuid4()
        ex = _stub_execution(task_id=task_id)
        session = _StubSession(get_returns=ex)
        client = TestClient(_app(session))

        resp = client.patch(
            f"/api/v1/task_executions/{ex.id}",
            json={"status": "invented-status"},
        )
        assert resp.status_code == 422


# ── GET tests ────────────────────────────────────────────────────────────


class TestListTaskExecutions:
    def test_returns_rows_for_task(self) -> None:
        task_id = uuid.uuid4()
        ex1 = _stub_execution(task_id=task_id, trigger="initial")
        ex2 = _stub_execution(task_id=task_id, trigger="coordinator-rework")
        session = _StubSession(scalars_returns=[ex1, ex2])
        client = TestClient(_app(session))

        resp = client.get(f"/api/v1/task_executions?task_id={task_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 2
        triggers = [r["trigger"] for r in body]
        assert "initial" in triggers
        assert "coordinator-rework" in triggers

    def test_returns_empty_list_when_none(self) -> None:
        session = _StubSession(scalars_returns=[])
        client = TestClient(_app(session))

        resp = client.get(f"/api/v1/task_executions?task_id={uuid.uuid4()}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_returns_rows_for_worker_label(self) -> None:
        """ADR-0089: the token harvester's window join lists by label."""
        ex = _stub_execution(task_id=uuid.uuid4(), worker_label="worker-team1-1")
        session = _StubSession(scalars_returns=[ex])
        client = TestClient(_app(session))

        resp = client.get("/api/v1/task_executions?worker_label=worker-team1-1")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["worker_label"] == "worker-team1-1"

    def test_422_when_no_filter_given(self) -> None:
        session = _StubSession(scalars_returns=[])
        client = TestClient(_app(session))

        resp = client.get("/api/v1/task_executions")
        assert resp.status_code == 422

    def test_status_filter_running_returns_only_running_rows(self) -> None:
        """?status=running must return only rows with status='running'.

        This is the ADR-0087 stale-sweep precision fix: the coordinator
        must pass ?status=running so it only marks in-flight rows as
        failed/coordinator_restart, never already-terminal ones.

        The stub is pre-loaded with a running execution only (mirroring
        what a real DB returns for WHERE status='running'); the endpoint
        must return exactly that row with status='running'.
        """
        task_id = uuid.uuid4()
        ex_running = _stub_execution(task_id=task_id, status="running")
        session = _StubSession(scalars_returns=[ex_running])
        client = TestClient(_app(session))

        resp = client.get(f"/api/v1/task_executions?task_id={task_id}&status=running")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["status"] == "running"

    def test_status_filter_invalid_value_returns_422(self) -> None:
        """An out-of-vocabulary ?status= value must return 422."""
        task_id = uuid.uuid4()
        session = _StubSession(scalars_returns=[])
        client = TestClient(_app(session))

        resp = client.get(
            f"/api/v1/task_executions?task_id={task_id}&status=not-a-real-status"
        )

        assert resp.status_code == 422

    def test_status_filter_completed_accepted(self) -> None:
        task_id = uuid.uuid4()
        ex = _stub_execution(task_id=task_id, status="completed")
        session = _StubSession(scalars_returns=[ex])
        client = TestClient(_app(session))

        resp = client.get(f"/api/v1/task_executions?task_id={task_id}&status=completed")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body[0]["status"] == "completed"

    def test_status_filter_omitted_returns_all_rows(self) -> None:
        """No ?status= param leaves all rows visible (backwards-compatible)."""
        task_id = uuid.uuid4()
        ex1 = _stub_execution(task_id=task_id, status="running")
        ex2 = _stub_execution(task_id=task_id, status="completed")
        session = _StubSession(scalars_returns=[ex1, ex2])
        client = TestClient(_app(session))

        resp = client.get(f"/api/v1/task_executions?task_id={task_id}")

        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 2


# ── Reconcile tests ─────────────────────────────────────────────────────


class TestReconcileCoordinatorRestart:
    def test_reconcile_returns_count_of_restored_rows(self) -> None:
        """POST reconcile-coordinator-restart returns {reconciled: N}."""
        session = _StubSession(rowcount=40)
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/task_executions/reconcile-coordinator-restart"
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"reconciled": 40}

    def test_reconcile_zero_when_no_rows_match(self) -> None:
        """Idempotent: a second call returns reconciled=0."""
        session = _StubSession(rowcount=0)
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/task_executions/reconcile-coordinator-restart"
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"reconciled": 0}
