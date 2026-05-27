"""Unit tests for ``POST /api/v1/dashboard/tasks/{task_id}/cancel``
(ADR-0056 PR-B4).

Exercises the route handler directly with stub session + stub dispatcher
— no live database, mirroring ``test_routers_dashboard_overview.py``.

Coverage:
  * Happy path  — non-terminal task → 202, ``task.cancelled`` event row
    is persisted via ``Dispatcher.persist_and_publish``.
  * 404         — ``task_id`` does not exist in ``tasks``.
  * 409         — task already terminal (cancelled / merged / done).
  * 422         — missing or empty ``reason``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import get_dispatcher
from treadmill_api.events.task import TaskCancelled
from treadmill_api.models.event import Event
from treadmill_api.routers.dashboard import router as dashboard_router


# ── Stubs ─────────────────────────────────────────────────────────────────────


class _StubResult:
    """Tiny stand-in for SQLAlchemy ``Result`` exposing only the slice the
    handler reads — ``.first()`` returning a mapping-style row (or
    ``None``)."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def first(self) -> Any:
        if self._row is None:
            return None
        # Pydantic / handler only reads attribute access (``row.action``);
        # a SimpleNamespace gives that without dragging in SQLAlchemy.
        from types import SimpleNamespace

        return SimpleNamespace(**self._row)


class _StubSession:
    """Routes ``session.execute(text(SQL), params)`` to fixtures by SQL
    substring — the cancel route issues two reads (task-existence,
    terminal-event) before handing off to the dispatcher.
    """

    def __init__(
        self,
        *,
        task_exists: bool = True,
        terminal_action: str | None = None,
    ) -> None:
        self.task_exists = task_exists
        self.terminal_action = terminal_action
        self.committed = False
        self.recorded_params: list[dict[str, Any] | None] = []

    async def execute(
        self, statement: Any, params: dict[str, Any] | None = None,
    ) -> _StubResult:
        self.recorded_params.append(params)
        sql = statement.text if hasattr(statement, "text") else str(statement)

        if "FROM tasks WHERE id" in sql:
            return _StubResult({"one": 1} if self.task_exists else None)
        if "FROM events" in sql and "entity_type = 'task'" in sql:
            if self.terminal_action is None:
                return _StubResult(None)
            return _StubResult({"action": self.terminal_action})
        raise AssertionError(f"unexpected SQL passed to stub session:\n{sql}")

    async def commit(self) -> None:
        self.committed = True


class _StubDispatcher:
    """Captures ``persist_and_publish`` calls and returns a fake Event row
    with a generated id — the handler echoes that id back in its 202
    response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def persist_and_publish(
        self,
        session: Any,
        *,
        entity_type: str,
        action: str,
        payload: Any,
        plan_id: Any = None,
        task_id: Any = None,
        run_id: Any = None,
        step_id: Any = None,
    ) -> Event:
        self.calls.append(
            {
                "entity_type": entity_type,
                "action": action,
                "payload": payload,
                "task_id": task_id,
            }
        )
        # The handler reads ``event.id`` off the returned row; stamp one so
        # the response model can serialize cleanly.
        return Event(
            id=uuid.uuid4(),
            entity_type=entity_type,
            action=action,
            task_id=task_id,
            payload=payload.model_dump(mode="json"),
        )


def _build_app(
    session: _StubSession, dispatcher: _StubDispatcher,
) -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)

    def _session_override() -> Iterator[_StubSession]:
        yield session

    def _dispatcher_override() -> _StubDispatcher:
        return dispatcher

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_dispatcher] = _dispatcher_override
    return app


# ── Happy path ────────────────────────────────────────────────────────────────


def test_cancel_task_happy_path_emits_task_cancelled_event() -> None:
    """Non-terminal task → 202, ``persist_and_publish`` called once with a
    ``TaskCancelled`` payload carrying the operator's reason, and the
    response echoes ``{event_id, task_id}``."""
    task_id = uuid.uuid4()
    session = _StubSession(task_exists=True, terminal_action=None)
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/dashboard/tasks/{task_id}/cancel",
            json={"reason": "operator: superseded by t-42"},
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["task_id"] == str(task_id)
    assert uuid.UUID(body["event_id"])  # parses as a UUID

    # One event-insertion call with the right shape.
    assert len(dispatcher.calls) == 1
    call = dispatcher.calls[0]
    assert call["entity_type"] == "task"
    assert call["action"] == "cancelled"
    assert call["task_id"] == task_id
    assert isinstance(call["payload"], TaskCancelled)
    assert call["payload"].reason == "operator: superseded by t-42"

    # The handler commits the transaction so the event row + any
    # callee-managed inserts land atomically.
    assert session.committed is True


# ── 404 ───────────────────────────────────────────────────────────────────────


def test_cancel_task_returns_404_when_task_does_not_exist() -> None:
    task_id = uuid.uuid4()
    session = _StubSession(task_exists=False)
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/dashboard/tasks/{task_id}/cancel",
            json={"reason": "stale"},
        )

    assert response.status_code == 404, response.text
    # No event written when the task lookup fails.
    assert dispatcher.calls == []
    assert session.committed is False


# ── 409 — task already terminal ───────────────────────────────────────────────


@pytest.mark.parametrize("terminal_action", ["cancelled", "merged", "done"])
def test_cancel_task_returns_409_when_task_already_terminal(
    terminal_action: str,
) -> None:
    """A prior ``task.cancelled`` / ``task.merged`` / ``task.done`` event
    short-circuits with 409 and does not insert a duplicate cancelled
    event."""
    task_id = uuid.uuid4()
    session = _StubSession(task_exists=True, terminal_action=terminal_action)
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/dashboard/tasks/{task_id}/cancel",
            json={"reason": "redundant"},
        )

    assert response.status_code == 409, response.text
    assert terminal_action in response.json()["detail"]
    assert dispatcher.calls == []
    assert session.committed is False


# ── 422 — Pydantic validation ─────────────────────────────────────────────────


def test_cancel_task_rejects_missing_reason() -> None:
    task_id = uuid.uuid4()
    session = _StubSession()
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/dashboard/tasks/{task_id}/cancel",
            json={},
        )

    assert response.status_code == 422, response.text
    # Body validation runs before any session interaction.
    assert dispatcher.calls == []


def test_cancel_task_rejects_empty_reason() -> None:
    """``reason`` carries ``min_length=1`` — an empty string is a 422,
    not a silent ``task.cancelled`` with an empty payload string."""
    task_id = uuid.uuid4()
    session = _StubSession()
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/dashboard/tasks/{task_id}/cancel",
            json={"reason": ""},
        )

    assert response.status_code == 422, response.text
    assert dispatcher.calls == []
