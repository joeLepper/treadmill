"""Unit tests for ``POST /api/v1/tasks/{task_id}/worker_hint_request`` (ADR-0081).

Tests the endpoint handler directly with stub async session + dispatcher.
Coverage:
  * Worker hint request succeeds and emits worker_hint_requested event
  * Task not found returns 404
  * Event payload is correctly formed with reason, context_excerpt, worker_step_id
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import get_dispatcher
from treadmill_api.events.task import TaskWorkerHintRequested
from treadmill_api.models import Task
from treadmill_api.routers.tasks import router as tasks_router


class _StubResult:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = SimpleNamespace(**row) if row is not None else None

    def one(self) -> Any:
        if self._row is None:
            raise Exception("No row")
        return self._row

    def one_or_none(self) -> Any | None:
        return self._row


class _StubSession:
    def __init__(self, task: dict[str, Any] | None = None):
        self.task = task

    async def get(self, model_class: type, task_id: uuid.UUID) -> Task | None:
        if self.task is None:
            return None
        task = Task(
            id=uuid.UUID(self.task["id"]),
            plan_id=uuid.UUID(self.task["plan_id"]),
            repo=self.task["repo"],
            title=self.task["title"],
            description=self.task.get("description"),
            created_by=self.task.get("created_by"),
        )
        return task

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _StubResult:
        row = dict(self.task or {})
        return _StubResult(row if row else None)

    async def commit(self) -> None:
        pass


class _StubDispatcher:
    def __init__(self) -> None:
        self.published_events: list[TaskWorkerHintRequested] = []

    async def persist_and_publish(
        self,
        session: Any,
        entity_type: str,
        action: str,
        payload: Any,
        plan_id: uuid.UUID,
        task_id: uuid.UUID,
    ) -> None:
        if action == "worker_hint_requested":
            self.published_events.append(payload)


_TASK_ID = "12345678-1234-1234-1234-123456789012"
_PLAN_ID = "87654321-4321-4321-4321-210987654321"
_WF_VERSION_ID = "abcdefab-cdef-abcd-efab-cdefabcdefab"


def _valid_task(**overrides) -> dict[str, Any]:
    base = {
        "id": _TASK_ID,
        "plan_id": _PLAN_ID,
        "repo": "test/repo",
        "title": "Test task",
        "description": "Test description",
        "workflow_version_id": _WF_VERSION_ID,
        "created_by": "operator-carla",
        "created_at": datetime.now(timezone.utc),
        "parent_task_id": None,
        "derived_status": "pr_open",
        "derived_mergeability": "unknown",
    }
    base.update(overrides)
    return base


def test_worker_hint_request_succeeds() -> None:
    """Worker hint request succeeds and emits event."""
    app = FastAPI()
    task = _valid_task()
    stub_session = _StubSession(task)
    stub_dispatcher = _StubDispatcher()

    async def override_session() -> AsyncIterator[_StubSession]:
        yield stub_session

    async def override_dispatcher() -> AsyncIterator[_StubDispatcher]:
        yield stub_dispatcher

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_dispatcher] = override_dispatcher
    app.include_router(tasks_router)

    client = TestClient(app)

    response = client.post(
        f"/api/v1/tasks/{_TASK_ID}/worker_hint_request",
        json={
            "reason": "tests_need_scope",
            "context_excerpt": "The test count is wrong",
            "worker_step_id": "step-456",
        },
    )

    assert response.status_code == 200
    assert len(stub_dispatcher.published_events) == 1
    event = stub_dispatcher.published_events[0]
    assert event.reason == "tests_need_scope"
    assert event.context_excerpt == "The test count is wrong"
    assert event.worker_step_id == "step-456"


def test_worker_hint_request_with_max_length_fields() -> None:
    """Worker hint request with max-length fields succeeds."""
    app = FastAPI()
    task = _valid_task()
    stub_session = _StubSession(task)
    stub_dispatcher = _StubDispatcher()

    async def override_session() -> AsyncIterator[_StubSession]:
        yield stub_session

    async def override_dispatcher() -> AsyncIterator[_StubDispatcher]:
        yield stub_dispatcher

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_dispatcher] = override_dispatcher
    app.include_router(tasks_router)

    client = TestClient(app)

    response = client.post(
        f"/api/v1/tasks/{_TASK_ID}/worker_hint_request",
        json={
            "reason": "x" * 100,  # max 100 chars
            "context_excerpt": "y" * 500,  # max 500 chars
            "worker_step_id": "step-" + "a" * 95,
        },
    )

    assert response.status_code == 200
    assert len(stub_dispatcher.published_events) == 1
    event = stub_dispatcher.published_events[0]
    assert event.reason == "x" * 100
    assert event.context_excerpt == "y" * 500


def test_worker_hint_request_task_not_found() -> None:
    """Unknown task_id returns 404."""
    app = FastAPI()
    stub_session = _StubSession(None)
    stub_dispatcher = _StubDispatcher()

    async def override_session() -> AsyncIterator[_StubSession]:
        yield stub_session

    async def override_dispatcher() -> AsyncIterator[_StubDispatcher]:
        yield stub_dispatcher

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_dispatcher] = override_dispatcher
    app.include_router(tasks_router)

    client = TestClient(app)

    response = client.post(
        f"/api/v1/tasks/{_TASK_ID}/worker_hint_request",
        json={
            "reason": "stuck",
            "context_excerpt": "Need help",
            "worker_step_id": "step-123",
        },
    )

    assert response.status_code == 404
    assert "task not found" in response.json()["detail"].lower()


def test_worker_hint_request_empty_reason_rejected() -> None:
    """Empty reason is rejected by validation."""
    app = FastAPI()
    task = _valid_task()
    stub_session = _StubSession(task)
    stub_dispatcher = _StubDispatcher()

    async def override_session() -> AsyncIterator[_StubSession]:
        yield stub_session

    async def override_dispatcher() -> AsyncIterator[_StubDispatcher]:
        yield stub_dispatcher

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_dispatcher] = override_dispatcher
    app.include_router(tasks_router)

    client = TestClient(app)

    response = client.post(
        f"/api/v1/tasks/{_TASK_ID}/worker_hint_request",
        json={
            "reason": "",
            "context_excerpt": "context",
            "worker_step_id": "step-123",
        },
    )

    assert response.status_code == 422  # Unprocessable Entity
