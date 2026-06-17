"""Unit tests for ``/api/v1/task_board`` (ADR-0084 §6 / Task 1C).

Three coverage axes:

  * **Upsert semantics**: PATCH creates a row on first call (with
    plan_id derived from the parent task), updates fields on subsequent
    calls, refreshes ``updated_at`` even on no-op payloads.
  * **Status vocabulary validation**: requests with unknown statuses
    return 422; requests with any of ``TASK_BOARD_STATUSES`` are
    accepted.
  * **FK enforcement at the model layer**: the model declares CASCADE
    delete from ``tasks`` and ``plans``; tests assert the model's
    foreign-key columns are present + correct.
  * **Reconciliation read path**: GET returns rows ordered by
    ``updated_at desc``.

Live-DB reconciliation against the task_status VIEW is exercised by the
integration test suite (``test_integration_task_board.py``) and run
under ``TREADMILL_INTEGRATION=1``. This file uses an in-memory stub so
it runs in the default unit-test pass.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.models.task_board import TASK_BOARD_STATUSES, TaskBoard
from treadmill_api.routers.task_board import router as task_board_router


# ── In-memory stub session ────────────────────────────────────────────────────


class _TaskRow:
    """Stand-in for the ORM Task with the one attribute the router reads."""

    def __init__(self, task_id: uuid.UUID, plan_id: uuid.UUID) -> None:
        self.id = task_id
        self.plan_id = plan_id


class _StubResult:
    def __init__(self, rows: list[TaskBoard]) -> None:
        self._rows = rows

    def scalars(self) -> "_StubResult":
        return self

    def all(self) -> list[TaskBoard]:
        return self._rows


class _StubSession:
    """Async-session shape sufficient for the router's GET/PATCH paths."""

    def __init__(self) -> None:
        self._tasks: dict[uuid.UUID, _TaskRow] = {}
        self._board: dict[uuid.UUID, TaskBoard] = {}
        self.commit_calls = 0

    # ── seeding for tests ─────────────────────────────────────────────────
    def seed_task(self, task_id: uuid.UUID, plan_id: uuid.UUID) -> None:
        self._tasks[task_id] = _TaskRow(task_id, plan_id)

    def seed_board(self, row: TaskBoard) -> None:
        self._board[row.task_id] = row

    # ── session API surface the router uses ───────────────────────────────
    async def get(self, model: Any, key: uuid.UUID) -> Any | None:
        if model.__name__ == "Task":
            return self._tasks.get(key)
        if model.__name__ == "TaskBoard":
            return self._board.get(key)
        raise AssertionError(f"unexpected get() for {model}")

    async def execute(self, stmt: Any) -> _StubResult:
        # Only one shape used: select(TaskBoard).where(plan_id == X).order_by(updated_at desc)
        # Extract plan_id from the compiled WHERE clause naively for the stub.
        plan_id = self._extract_plan_id(stmt)
        rows = [r for r in self._board.values() if r.plan_id == plan_id]
        rows.sort(key=lambda r: r.updated_at, reverse=True)
        return _StubResult(rows)

    def add(self, row: TaskBoard) -> None:
        self._board[row.task_id] = row

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        pass

    async def refresh(self, row: TaskBoard) -> None:
        # No-op for the stub.
        pass

    # ── helpers ───────────────────────────────────────────────────────────
    def _extract_plan_id(self, stmt: Any) -> uuid.UUID:
        # Pulls the first UUID-typed bound parameter from the compiled statement.
        # Used by the GET path which is select(...).where(plan_id == X).
        compiled = stmt.compile(compile_kwargs={"literal_binds": False})
        for param in compiled.params.values():
            if isinstance(param, uuid.UUID):
                return param
        raise AssertionError("plan_id parameter not found in statement")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def app_and_session() -> tuple[FastAPI, _StubSession]:
    session = _StubSession()
    app = FastAPI()
    app.include_router(task_board_router)

    async def _override() -> _StubSession:
        return session

    app.dependency_overrides[get_session] = _override
    return app, session


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_patch_creates_row_on_first_call(app_and_session) -> None:
    app, session = app_and_session
    plan_id = uuid.uuid4()
    task_id = uuid.uuid4()
    session.seed_task(task_id, plan_id)

    client = TestClient(app)
    resp = client.patch(
        f"/api/v1/task_board/{task_id}",
        json={
            "status": "in_flight",
            "assignee": "treadmill-bert",
            "branch": "feat/x",
            "updated_by": "coordinator-ramjac",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task_id)
    assert body["plan_id"] == str(plan_id)
    assert body["status"] == "in_flight"
    assert body["assignee"] == "treadmill-bert"
    assert body["branch"] == "feat/x"
    assert body["updated_by"] == "coordinator-ramjac"
    assert session.commit_calls == 1


def test_patch_first_insert_requires_status(app_and_session) -> None:
    app, session = app_and_session
    plan_id = uuid.uuid4()
    task_id = uuid.uuid4()
    session.seed_task(task_id, plan_id)

    client = TestClient(app)
    resp = client.patch(
        f"/api/v1/task_board/{task_id}",
        json={"assignee": "treadmill-bert"},
    )
    assert resp.status_code == 422
    assert "status" in resp.text.lower()


def test_patch_updates_selectively(app_and_session) -> None:
    app, session = app_and_session
    plan_id = uuid.uuid4()
    task_id = uuid.uuid4()
    session.seed_task(task_id, plan_id)
    existing = TaskBoard(
        task_id=task_id,
        plan_id=plan_id,
        assignee="treadmill-bert",
        status="in_flight",
        branch="feat/x",
        pr_number=None,
        notes=None,
        updated_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        updated_by="coordinator-ramjac",
    )
    session.seed_board(existing)

    client = TestClient(app)
    # Update only pr_number; status/branch/assignee should remain.
    resp = client.patch(
        f"/api/v1/task_board/{task_id}",
        json={"pr_number": 1234},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pr_number"] == 1234
    assert body["status"] == "in_flight"  # unchanged
    assert body["assignee"] == "treadmill-bert"  # unchanged
    assert body["branch"] == "feat/x"  # unchanged


def test_patch_clears_assignee_explicitly(app_and_session) -> None:
    """Sending ``"assignee": null`` is distinct from omitting the key —
    null clears, omitted leaves untouched."""
    app, session = app_and_session
    plan_id = uuid.uuid4()
    task_id = uuid.uuid4()
    session.seed_task(task_id, plan_id)
    existing = TaskBoard(
        task_id=task_id,
        plan_id=plan_id,
        assignee="treadmill-bert",
        status="in_flight",
        branch=None,
        pr_number=None,
        notes=None,
        updated_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        updated_by="coordinator-ramjac",
    )
    session.seed_board(existing)

    client = TestClient(app)
    resp = client.patch(f"/api/v1/task_board/{task_id}", json={"assignee": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["assignee"] is None


def test_patch_unknown_status_returns_422(app_and_session) -> None:
    app, session = app_and_session
    plan_id = uuid.uuid4()
    task_id = uuid.uuid4()
    session.seed_task(task_id, plan_id)

    client = TestClient(app)
    resp = client.patch(
        f"/api/v1/task_board/{task_id}",
        json={"status": "totally_made_up"},
    )
    assert resp.status_code == 422
    assert "status" in resp.text.lower()


def test_patch_returns_404_for_unknown_task(app_and_session) -> None:
    app, _session = app_and_session
    client = TestClient(app)
    resp = client.patch(
        f"/api/v1/task_board/{uuid.uuid4()}",
        json={"status": "ready"},
    )
    assert resp.status_code == 404


def test_get_returns_rows_for_plan_in_order(app_and_session) -> None:
    app, session = app_and_session
    plan_id = uuid.uuid4()
    other_plan_id = uuid.uuid4()

    task_a = uuid.uuid4()
    task_b = uuid.uuid4()
    task_other = uuid.uuid4()

    session.seed_board(
        TaskBoard(
            task_id=task_a,
            plan_id=plan_id,
            assignee=None,
            status="ready",
            branch=None,
            pr_number=None,
            notes=None,
            updated_at=datetime(2026, 6, 8, 11, 0, tzinfo=timezone.utc),
            updated_by=None,
        )
    )
    session.seed_board(
        TaskBoard(
            task_id=task_b,
            plan_id=plan_id,
            assignee="treadmill-bert",
            status="in_flight",
            branch="feat/y",
            pr_number=None,
            notes=None,
            updated_at=datetime(2026, 6, 8, 13, 0, tzinfo=timezone.utc),
            updated_by=None,
        )
    )
    session.seed_board(
        TaskBoard(
            task_id=task_other,
            plan_id=other_plan_id,
            assignee=None,
            status="ready",
            branch=None,
            pr_number=None,
            notes=None,
            updated_at=datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc),
            updated_by=None,
        )
    )

    client = TestClient(app)
    resp = client.get(f"/api/v1/task_board/{plan_id}")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    # Only the two rows for plan_id, ordered by updated_at desc.
    assert [r["task_id"] for r in rows] == [str(task_b), str(task_a)]


def test_get_empty_for_unknown_plan(app_and_session) -> None:
    app, _session = app_and_session
    client = TestClient(app)
    resp = client.get(f"/api/v1/task_board/{uuid.uuid4()}")
    assert resp.status_code == 200
    assert resp.json() == []


def test_all_statuses_in_vocabulary_accepted(app_and_session) -> None:
    """Every value in ``TASK_BOARD_STATUSES`` must round-trip through PATCH.

    Guards against vocab evolution silently failing — adding a status to
    the frozenset without thinking through the validator is a common
    next-step risk."""
    app, session = app_and_session
    plan_id = uuid.uuid4()

    client = TestClient(app)
    for status_name in sorted(TASK_BOARD_STATUSES):
        task_id = uuid.uuid4()
        session.seed_task(task_id, plan_id)
        resp = client.patch(
            f"/api/v1/task_board/{task_id}",
            json={"status": status_name},
        )
        assert resp.status_code == 200, f"{status_name}: {resp.text}"
        assert resp.json()["status"] == status_name


def test_model_declares_cascade_fks() -> None:
    """Model invariant: task_board cascades on tasks/plans delete.

    Schema-level FK check — guards against a refactor that breaks the
    invariant. The migration's CASCADE is the source of truth in the DB;
    this test asserts the model declaration matches.
    """
    task_id_col = TaskBoard.__table__.c.task_id
    plan_id_col = TaskBoard.__table__.c.plan_id

    task_fks = list(task_id_col.foreign_keys)
    plan_fks = list(plan_id_col.foreign_keys)

    assert len(task_fks) == 1
    assert task_fks[0].column.table.name == "tasks"
    assert task_fks[0].ondelete == "CASCADE"

    assert len(plan_fks) == 1
    assert plan_fks[0].column.table.name == "plans"
    assert plan_fks[0].ondelete == "CASCADE"
