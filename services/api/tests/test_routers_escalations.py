"""Unit tests for ``/api/v1/escalations`` (ADR-0062 Step 3).

Exercises the route handlers with stub session + stub dispatcher — no
live database. Same pattern as ``test_routers_dashboard_ack_escalation.py``
+ ``test_routers_dashboard_cancel.py``: route SQL by substring to canned
fixture rows, mock the dispatcher to capture the typed event payloads.

Coverage:

  * ``GET  /api/v1/escalations``                — open-incident listing +
    ``?reason=`` filter + ``?task=`` prefix filter.
  * ``POST /api/v1/escalations/{task_id}/close`` — happy path emits
    ``escalation_closed`` with ``close_reason='operator_close'`` + correct
    MTTR, 404 missing task, 409 no open incident, 409 already closed.
  * ``POST /api/v1/escalations/{task_id}/ack``   — happy path emits
    ``escalation_acknowledged`` via the dispatcher, 404, 409.
  * ``GET  /api/v1/escalations/report``         — bucketing by reason /
    day / task; MTTR percentiles; empty window.

``GET /stream`` is structurally untestable here (it consumes an
in-process publisher subscription); the contract is shape-pinned by the
SSE-frame helper test below.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import get_dispatcher
from treadmill_api.events.task import TaskEscalationAcknowledged, TaskEscalationClosed
from treadmill_api.models.event import Event
from treadmill_api.routers.escalations import (
    _comment,
    _percentile,
    _sse_frame,
    router as escalations_router,
)


# ── Stubs ────────────────────────────────────────────────────────────────────


class _MappingsResult:
    """``.mappings().all()`` returns a list of mapping rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_MappingsResult":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _RowResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def one(self) -> Any:
        return self._row


class _StubSession:
    """Routes ``session.execute(text(SQL), params)`` to canned results by
    SQL-text substring. Each test wires up only the responses the handler
    actually issues.
    """

    def __init__(
        self,
        *,
        open_rows: list[dict[str, Any]] | None = None,
        task_exists: bool = True,
        open_for_task: tuple[datetime | None, datetime | None] = (None, None),
        closed_events: list[dict[str, Any]] | None = None,
        new_event_id: uuid.UUID | None = None,
    ) -> None:
        self.open_rows = open_rows or []
        self.task_exists = task_exists
        self.open_for_task = open_for_task
        self.closed_events = closed_events or []
        self.new_event_id = new_event_id or uuid.uuid4()
        self.commit_count = 0

    async def execute(
        self, statement: Any, params: dict[str, Any] | None = None,
    ) -> Any:
        sql = statement.text if hasattr(statement, "text") else str(statement)

        if "FROM tasks WHERE id" in sql:
            return _ScalarResult(1 if self.task_exists else None)

        if "last_escalation" in sql and "last_close" in sql and "last_ack" in sql:
            # _OPEN_SQL — list endpoint.
            return _MappingsResult(self.open_rows)

        if "last_escalation" in sql and "last_close" in sql:
            # _OPEN_FOR_TASK_SQL — close/ack endpoints.
            opened_at, closed_at = self.open_for_task
            return _RowResult(
                SimpleNamespace(opened_at=opened_at, closed_at=closed_at),
            )

        if (
            "FROM events" in sql
            and "action = 'escalation_closed'" in sql
            and "mttr_seconds" in sql
        ):
            return _MappingsResult(self.closed_events)

        raise AssertionError(f"unexpected SQL passed to stub session:\n{sql}")

    async def commit(self) -> None:
        self.commit_count += 1

    async def get(self, model: Any, task_id: uuid.UUID) -> Any:
        # Used by the shared ``emit_operator_close`` helper through
        # ``escalation_close_sweep`` — it does a ``session.get(Task, …)``
        # before persisting. We hand back a stub with the plan id field
        # the helper reads off.
        return SimpleNamespace(id=task_id, plan_id=uuid.uuid4())


class _StubDispatcher:
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
        return Event(
            id=uuid.uuid4(),
            entity_type=entity_type,
            action=action,
            task_id=task_id,
            payload=payload.model_dump(mode="json"),
        )


def _build_app(
    session: _StubSession, dispatcher: _StubDispatcher | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(escalations_router)

    def _session_override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _session_override
    if dispatcher is not None:
        def _dispatcher_override() -> _StubDispatcher:
            return dispatcher

        app.dependency_overrides[get_dispatcher] = _dispatcher_override
    return app


def _open_row(
    *,
    task_id: str | None = None,
    repo: str = "joeLepper/treadmill",
    title: str = "fix the thing",
    opened_at: datetime | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id or str(uuid.uuid4()),
        "repo": repo,
        "title": title,
        "opened_at": opened_at or datetime.now(timezone.utc),
        "reason": reason,
    }


# ── GET /api/v1/escalations ──────────────────────────────────────────────────


def test_list_open_escalations_returns_all_rows_unfiltered() -> None:
    rows = [
        _open_row(reason="architect_cap"),
        _open_row(reason="stuck_task_sweep"),
        _open_row(reason="gate-broken"),
    ]
    session = _StubSession(open_rows=rows)
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get("/api/v1/escalations")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 3
    assert {r["reason"] for r in body} == {
        "architect_cap", "stuck_task_sweep", "gate-broken",
    }


def test_list_open_escalations_returns_empty_when_none_open() -> None:
    session = _StubSession(open_rows=[])
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get("/api/v1/escalations")

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_list_open_escalations_reason_filter_narrows_rows() -> None:
    rows = [
        _open_row(reason="architect_cap"),
        _open_row(reason="stuck_task_sweep"),
        _open_row(reason="gate-broken"),
    ]
    session = _StubSession(open_rows=rows)
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/escalations", params={"reason": "architect_cap"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["reason"] == "architect_cap"


def test_list_open_escalations_task_prefix_filter_narrows_rows() -> None:
    task_a = "aaaaaaaa-0000-0000-0000-000000000001"
    task_b = "bbbbbbbb-0000-0000-0000-000000000001"
    rows = [
        _open_row(task_id=task_a, reason="architect_cap"),
        _open_row(task_id=task_b, reason="architect_cap"),
    ]
    session = _StubSession(open_rows=rows)
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/escalations", params={"task": "aa"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["task_id"] == task_a


def test_list_open_escalations_rejects_unknown_reason_with_422() -> None:
    session = _StubSession(open_rows=[])
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/escalations", params={"reason": "not-a-real-reason"},
        )

    assert response.status_code == 422, response.text


# ── POST /api/v1/escalations/{task_id}/close ─────────────────────────────────


def test_close_escalation_emits_operator_close_and_returns_mttr() -> None:
    task_id = uuid.uuid4()
    opened_at = datetime.now(timezone.utc) - timedelta(minutes=42)
    session = _StubSession(
        task_exists=True, open_for_task=(opened_at, None),
    )
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(f"/api/v1/escalations/{task_id}/close")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["task_id"] == str(task_id)
    assert body["close_reason"] == "operator_close"
    # 42 minutes back, within a few seconds — assert a sane lower bound.
    assert body["mttr_seconds"] >= 41 * 60

    # One event emitted, with the right shape.
    assert len(dispatcher.calls) == 1
    call = dispatcher.calls[0]
    assert call["entity_type"] == "task"
    assert call["action"] == "escalation_closed"
    assert call["task_id"] == task_id
    payload = call["payload"]
    assert isinstance(payload, TaskEscalationClosed)
    assert payload.close_reason == "operator_close"
    assert payload.opened_at == opened_at

    assert session.commit_count == 1


def test_close_escalation_404_when_task_missing() -> None:
    task_id = uuid.uuid4()
    session = _StubSession(task_exists=False)
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(f"/api/v1/escalations/{task_id}/close")

    assert response.status_code == 404, response.text
    assert dispatcher.calls == []
    assert session.commit_count == 0


def test_close_escalation_409_when_no_open_incident() -> None:
    task_id = uuid.uuid4()
    session = _StubSession(
        task_exists=True, open_for_task=(None, None),
    )
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(f"/api/v1/escalations/{task_id}/close")

    assert response.status_code == 409, response.text
    assert "no open escalation" in response.json()["detail"]
    assert dispatcher.calls == []
    assert session.commit_count == 0


def test_close_escalation_409_when_already_closed() -> None:
    task_id = uuid.uuid4()
    opened_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    closed_at = opened_at + timedelta(minutes=5)
    session = _StubSession(
        task_exists=True, open_for_task=(opened_at, closed_at),
    )
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(f"/api/v1/escalations/{task_id}/close")

    assert response.status_code == 409, response.text
    assert "already closed" in response.json()["detail"]
    assert dispatcher.calls == []


# ── POST /api/v1/escalations/{task_id}/ack ───────────────────────────────────


def test_ack_escalation_emits_acknowledged_event() -> None:
    task_id = uuid.uuid4()
    opened_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    session = _StubSession(
        task_exists=True, open_for_task=(opened_at, None),
    )
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(f"/api/v1/escalations/{task_id}/ack")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["task_id"] == str(task_id)
    assert uuid.UUID(body["event_id"])

    assert len(dispatcher.calls) == 1
    call = dispatcher.calls[0]
    assert call["entity_type"] == "task"
    assert call["action"] == "escalation_acknowledged"
    assert call["task_id"] == task_id
    assert isinstance(call["payload"], TaskEscalationAcknowledged)
    assert session.commit_count == 1


def test_ack_escalation_404_when_task_missing() -> None:
    task_id = uuid.uuid4()
    session = _StubSession(task_exists=False)
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(f"/api/v1/escalations/{task_id}/ack")

    assert response.status_code == 404, response.text
    assert dispatcher.calls == []


def test_ack_escalation_409_when_no_open_incident() -> None:
    task_id = uuid.uuid4()
    session = _StubSession(
        task_exists=True, open_for_task=(None, None),
    )
    dispatcher = _StubDispatcher()
    app = _build_app(session, dispatcher)

    with TestClient(app) as client:
        response = client.post(f"/api/v1/escalations/{task_id}/ack")

    assert response.status_code == 409, response.text
    assert "not currently escalated" in response.json()["detail"]
    assert dispatcher.calls == []


# ── GET /api/v1/escalations/report ───────────────────────────────────────────


def _closed_event(
    *,
    task_id: str | None = None,
    closed_at: datetime | None = None,
    close_reason: str = "operator_close",
    mttr_seconds: int = 300,
) -> dict[str, Any]:
    return {
        "task_id": task_id or str(uuid.uuid4()),
        "closed_at": closed_at or datetime.now(timezone.utc),
        "close_reason": close_reason,
        "mttr_seconds": mttr_seconds,
    }


def test_report_by_reason_buckets_and_orders_by_count_desc() -> None:
    events = [
        _closed_event(close_reason="operator_close", mttr_seconds=60),
        _closed_event(close_reason="operator_close", mttr_seconds=180),
        _closed_event(close_reason="re_progressed", mttr_seconds=240),
        _closed_event(close_reason="pr_merged", mttr_seconds=600),
    ]
    session = _StubSession(closed_events=events)
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/escalations/report", params={"by": "reason"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["by"] == "reason"
    assert body["total"] == 4

    buckets = body["buckets"]
    # operator_close has 2 events; appears first.
    assert buckets[0]["key"] == "operator_close"
    assert buckets[0]["count"] == 2
    # avg of [60, 180] = 120
    assert buckets[0]["mttr_seconds_avg"] == 120
    keys = {b["key"] for b in buckets}
    assert keys == {"operator_close", "re_progressed", "pr_merged"}


def test_report_by_day_groups_events_per_utc_date() -> None:
    day_a = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
    day_b = datetime(2026, 6, 2, 8, 15, tzinfo=timezone.utc)
    events = [
        _closed_event(closed_at=day_a, mttr_seconds=100),
        _closed_event(closed_at=day_a, mttr_seconds=200),
        _closed_event(closed_at=day_b, mttr_seconds=500),
    ]
    session = _StubSession(closed_events=events)
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/escalations/report", params={"by": "day"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    buckets = {b["key"]: b for b in body["buckets"]}
    assert "2026-06-01" in buckets
    assert "2026-06-02" in buckets
    assert buckets["2026-06-01"]["count"] == 2
    assert buckets["2026-06-02"]["count"] == 1


def test_report_by_task_groups_events_per_task_id() -> None:
    task_a = str(uuid.uuid4())
    task_b = str(uuid.uuid4())
    events = [
        _closed_event(task_id=task_a, mttr_seconds=60),
        _closed_event(task_id=task_a, mttr_seconds=120),
        _closed_event(task_id=task_b, mttr_seconds=900),
    ]
    session = _StubSession(closed_events=events)
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/escalations/report", params={"by": "task"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    buckets = {b["key"]: b for b in body["buckets"]}
    assert task_a in buckets
    assert task_b in buckets
    assert buckets[task_a]["count"] == 2
    assert buckets[task_a]["mttr_seconds_avg"] == 90


def test_report_empty_window_returns_zero_buckets() -> None:
    session = _StubSession(closed_events=[])
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get("/api/v1/escalations/report")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0
    assert body["buckets"] == []


def test_report_rejects_invalid_by_with_422() -> None:
    session = _StubSession(closed_events=[])
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/escalations/report", params={"by": "decade"},
        )

    assert response.status_code == 422, response.text


# ── SSE frame helpers ────────────────────────────────────────────────────────


def test_sse_frame_renders_double_newline_terminated_data_line() -> None:
    """The SSE wire format is ``data: <json>\\n\\n``; pin the shape so a
    future change doesn't silently break the CLI ``tail`` parser."""
    frame = _sse_frame({"action": "escalated_to_operator", "id": "abc"})
    assert frame.endswith(b"\n\n")
    assert frame.startswith(b"data: ")
    # JSON payload round-trips.
    import json as _json

    body = _json.loads(frame[len(b"data: "): -2])
    assert body == {"action": "escalated_to_operator", "id": "abc"}


def test_sse_comment_renders_with_leading_colon() -> None:
    assert _comment("ping") == b": ping\n\n"


# ── _percentile helper ──────────────────────────────────────────────────────


def test_percentile_returns_zero_for_empty_input() -> None:
    assert _percentile([], 50) == 0


def test_percentile_nearest_rank_matches_spec() -> None:
    # Nearest-rank p50 of [10, 20, 30, 40, 50] is the 3rd element = 30.
    assert _percentile([10, 20, 30, 40, 50], 50) == 30
    # p95 of a 5-item list lands on the 5th item.
    assert _percentile([10, 20, 30, 40, 50], 95) == 50


# ── App-mount smoke ──────────────────────────────────────────────────────────


def test_app_mounts_escalations_router() -> None:
    """``app.py`` includes the escalations router exactly once."""
    from treadmill_api.app import create_app

    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/v1/escalations" in paths
    assert "/api/v1/escalations/stream" in paths
    assert "/api/v1/escalations/{task_id}/close" in paths
    assert "/api/v1/escalations/{task_id}/ack" in paths
    assert "/api/v1/escalations/report" in paths
