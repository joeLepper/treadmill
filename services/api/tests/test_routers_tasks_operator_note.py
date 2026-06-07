"""Unit tests for ``POST /api/v1/tasks/{task_id}/operator_note`` (ADR-0081).

Tests the endpoint handler directly with stub async session + dispatcher.
Coverage:
  * Setting a note succeeds and emits operator_hint_set event
  * Clearing a note (null) succeeds and emits operator_hint_set event
  * Task not found returns 404
  * Response includes updated task record
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import get_dispatcher
from treadmill_api.events.task import OperatorHintSet
from treadmill_api.models import Task
from treadmill_api.routers.tasks import router as tasks_router


class _StubResult:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        # Wrap the row dict in a SimpleNamespace so endpoint code that
        # accesses fields via attribute syntax (row.id, row.repo, ...)
        # works against the stub. The real SQLAlchemy Row object exposes
        # attribute access too.
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
        self.task_updated = False

    async def get(self, model_class: type, task_id: uuid.UUID) -> Task | None:
        if self.task is None:
            return None
        # Create a stub Task object
        task = Task(
            id=uuid.UUID(self.task["id"]),
            plan_id=uuid.UUID(self.task["plan_id"]),
            repo=self.task["repo"],
            title=self.task["title"],
            description=self.task.get("description"),
            workflow_version_id=uuid.UUID(self.task["workflow_version_id"]),
            created_by=self.task.get("created_by"),
            operator_note=self.task.get("operator_note"),
        )
        return task

    async def flush(self) -> None:
        self.task_updated = True

    async def refresh(self, obj: Task) -> None:
        if self.task is not None:
            obj.operator_note = self.task.get("operator_note")

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _StubResult:
        # Return task with updated operator_note
        row = dict(self.task or {})
        return _StubResult(row if row else None)

    async def commit(self) -> None:
        pass


class _StubDispatcher:
    def __init__(self) -> None:
        self.published_events: list[OperatorHintSet] = []

    async def persist_and_publish(
        self,
        session: Any,
        entity_type: str,
        action: str,
        payload: Any,
        plan_id: uuid.UUID,
        task_id: uuid.UUID,
    ) -> None:
        if action == "operator_hint_set":
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
        "created_by": "test-operator",
        "operator_note": None,
        "created_at": datetime.now(timezone.utc),
        "parent_task_id": None,
        "derived_status": "pr_open",
        "derived_mergeability": "unknown",
    }
    base.update(overrides)
    return base


def test_set_operator_note() -> None:
    """Setting a note succeeds and returns updated task."""
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
        f"/api/v1/tasks/{_TASK_ID}/operator_note",
        json={"note": "This is a test hint"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["id"] == _TASK_ID
    assert len(stub_dispatcher.published_events) == 1
    assert stub_dispatcher.published_events[0].note_excerpt == "This is a test hint"


def test_clear_operator_note() -> None:
    """Clearing a note (null) succeeds and emits event with (cleared) marker."""
    app = FastAPI()
    task = _valid_task(operator_note="Old hint text")
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
        f"/api/v1/tasks/{_TASK_ID}/operator_note",
        json={"note": None},
    )

    assert response.status_code == 200
    assert len(stub_dispatcher.published_events) == 1
    # When cleared, the excerpt should be marked as "(cleared)"
    assert stub_dispatcher.published_events[0].note_excerpt == "(cleared)"


def test_task_not_found() -> None:
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
        f"/api/v1/tasks/{_TASK_ID}/operator_note",
        json={"note": "hint"},
    )

    assert response.status_code == 404
    assert "task not found" in response.json()["detail"].lower()
