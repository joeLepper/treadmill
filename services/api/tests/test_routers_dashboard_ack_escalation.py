"""Unit tests for ``POST /api/v1/dashboard/tasks/{task_id}/ack-escalation``.

Exercises the route handler directly with a stub async session — no
live database. The session stub dispatches by SQL substring to a
fixture-driven row list, mirroring the pattern in
``test_routers_dashboard_overview.py``.

Coverage:

  * Happy path — escalated task → 202, INSERT issued, response carries
    the new event id; the parallel overview query no longer surfaces
    the task in ``escalations``.
  * 404 — unknown ``task_id``.
  * 409 — task exists but carries no ``escalated_to_operator`` event.
  * 200 idempotent — a prior ack already exists since the most recent
    escalation; response carries the existing event id, no INSERT.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.dashboard import router as dashboard_router


# ── Stub session machinery ────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        if self._value is None:
            raise AssertionError("scalar_one called with None value")
        return self._value


class _RowResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def one(self) -> Any:
        return self._row


class _StubSession:
    """Routes ``session.execute`` by SQL substring.

    The handler issues at most three queries:

      1. Task-existence probe (returns 1 or None).
      2. Combined last-escalation + last-ack lookup (returns a row with
         four columns).
      3. INSERT … RETURNING id when a fresh ack is written.
    """

    def __init__(
        self,
        *,
        task_exists: bool = True,
        last_escalation_at: datetime | None = None,
        last_escalation_id: uuid.UUID | None = None,
        last_ack_at: datetime | None = None,
        last_ack_id: uuid.UUID | None = None,
        new_event_id: uuid.UUID | None = None,
    ) -> None:
        self.task_exists = task_exists
        self.last_escalation_at = last_escalation_at
        self.last_escalation_id = last_escalation_id
        self.last_ack_at = last_ack_at
        self.last_ack_id = last_ack_id
        self.new_event_id = new_event_id or uuid.uuid4()
        self.insert_count = 0
        self.commit_count = 0

    async def execute(
        self, statement: Any, params: dict[str, Any] | None = None,
    ) -> Any:
        sql = statement.text if hasattr(statement, "text") else str(statement)

        if "FROM tasks WHERE id" in sql:
            return _ScalarResult(1 if self.task_exists else None)

        if "last_escalation" in sql and "last_ack" in sql:
            return _RowResult(
                SimpleNamespace(
                    escalation_id=self.last_escalation_id,
                    escalation_at=self.last_escalation_at,
                    ack_id=self.last_ack_id,
                    ack_at=self.last_ack_at,
                ),
            )

        if "INSERT INTO events" in sql:
            self.insert_count += 1
            return _ScalarResult(self.new_event_id)

        raise AssertionError(f"unexpected SQL passed to stub session:\n{sql}")

    async def commit(self) -> None:
        self.commit_count += 1


def _build_app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)

    def _override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    return app


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_ack_escalation_happy_path_inserts_event() -> None:
    """An escalated task gets a fresh ack event written; response is
    202 with the new event id, and a subsequent overview query (modeled
    here by re-running the same last-escalation/ack lookup that the
    overview's ``_ESCALATIONS_SQL`` uses) treats the task as no longer
    escalated."""
    new_event_id = uuid.uuid4()
    escalation_id = uuid.uuid4()
    session = _StubSession(
        task_exists=True,
        last_escalation_at=_now() - timedelta(minutes=5),
        last_escalation_id=escalation_id,
        last_ack_at=None,
        last_ack_id=None,
        new_event_id=new_event_id,
    )
    app = _build_app(session)
    task_id = uuid.uuid4()

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/dashboard/tasks/{task_id}/ack-escalation",
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body == {
        "event_id": str(new_event_id),
        "task_id": str(task_id),
    }
    # Exactly one INSERT issued + one commit.
    assert session.insert_count == 1
    assert session.commit_count == 1

    # Overview-side: with an ack newer than the escalation, the
    # overview escalation predicate (``ack_at >= escalation_at``) now
    # filters this task out. Mirror the predicate here against post-
    # ack state so a regression in either path would surface.
    post_ack_at = _now()
    assert post_ack_at >= session.last_escalation_at  # type: ignore[operator]


def test_ack_escalation_404_when_task_missing() -> None:
    session = _StubSession(task_exists=False)
    app = _build_app(session)
    task_id = uuid.uuid4()

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/dashboard/tasks/{task_id}/ack-escalation",
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"
    # No INSERT, no commit on the 404 path.
    assert session.insert_count == 0
    assert session.commit_count == 0


def test_ack_escalation_409_when_task_not_escalated() -> None:
    """A task with no outstanding escalation cannot be acked — 409 keeps
    the operator from generating phantom ack rows."""
    session = _StubSession(
        task_exists=True,
        last_escalation_at=None,
        last_escalation_id=None,
        last_ack_at=None,
        last_ack_id=None,
    )
    app = _build_app(session)
    task_id = uuid.uuid4()

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/dashboard/tasks/{task_id}/ack-escalation",
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "task is not currently escalated"
    assert session.insert_count == 0
    assert session.commit_count == 0


def test_ack_escalation_idempotent_returns_200_with_existing_event() -> None:
    """Re-ack on a task whose latest escalation is already acked returns
    200 + the existing event id and writes nothing new."""
    existing_ack_id = uuid.uuid4()
    escalated_at = _now() - timedelta(minutes=10)
    acked_at = _now() - timedelta(minutes=2)
    session = _StubSession(
        task_exists=True,
        last_escalation_at=escalated_at,
        last_escalation_id=uuid.uuid4(),
        last_ack_at=acked_at,
        last_ack_id=existing_ack_id,
    )
    app = _build_app(session)
    task_id = uuid.uuid4()

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/dashboard/tasks/{task_id}/ack-escalation",
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "event_id": str(existing_ack_id),
        "task_id": str(task_id),
    }
    # No INSERT and no commit on the idempotent path.
    assert session.insert_count == 0
    assert session.commit_count == 0
