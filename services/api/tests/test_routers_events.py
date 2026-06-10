"""Unit tests for ``POST /api/v1/events`` — the ADR-0086 §12.4 Path B
manual-event surface for the coordinator's webhook-backstop flow.

In-memory stub session keeps the suite hermetic; the dispatcher is
mocked via FastAPI's ``dependency_overrides`` so the test exercises
the router's persistence + idempotency logic without an SNS hop.

Coverage axes (per the brief)
=============================

* POST creates a row + returns 201 + body.
* 404 when ``task_id`` does not resolve.
* 409 when an identical ``(entity_type, action, task_id)`` row
  already exists in the events log.
* Idempotency triple is independent of payload contents — same
  triple, different payload still 409s.
* Generic passthrough — any ``entity_type``/``action`` round-trips.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher
from treadmill_api.models import Event, Task
from treadmill_api.routers.events import router as events_router


# ── Stubs ─────────────────────────────────────────────────────────────────


class _StubTask:
    """Minimal Task stand-in — the router only reads ``id``."""

    def __init__(self, task_id: uuid.UUID) -> None:
        self.id = task_id


class _StubEvent:
    """Stand-in for an Event row returned by the dispatcher mock."""

    def __init__(
        self,
        *,
        entity_type: str,
        action: str,
        task_id: uuid.UUID | None,
        plan_id: uuid.UUID | None,
        payload: dict[str, Any],
    ) -> None:
        self.id = uuid.uuid4()
        self.entity_type = entity_type
        self.action = action
        self.task_id = task_id
        self.plan_id = plan_id
        self.payload = payload
        self.created_at = datetime.now(timezone.utc)


class _StubSession:
    """In-memory session covering session.get + select(Event).where(...)."""

    def __init__(self) -> None:
        self._tasks: dict[uuid.UUID, _StubTask] = {}
        self._events: list[_StubEvent] = []
        self.commit_calls = 0

    # ── Seeding helpers ──────────────────────────────────────
    def seed_task(self) -> uuid.UUID:
        task_id = uuid.uuid4()
        self._tasks[task_id] = _StubTask(task_id)
        return task_id

    def seed_event(
        self,
        *,
        entity_type: str,
        action: str,
        task_id: uuid.UUID | None,
        payload: dict[str, Any] | None = None,
    ) -> _StubEvent:
        event = _StubEvent(
            entity_type=entity_type,
            action=action,
            task_id=task_id,
            plan_id=None,
            payload=payload or {},
        )
        self._events.append(event)
        return event

    # ── AsyncSession surface ─────────────────────────────────
    async def get(self, model: type, pk: Any) -> Any:
        if model is Task:
            return self._tasks.get(pk)
        return None

    async def execute(self, stmt: Any) -> "_StubResult":
        compiled = str(stmt)
        if "FROM events" not in compiled:
            return _StubResult(None)
        params = _bound_params(stmt)
        entity_type = _by_prefix(params, "entity_type")
        action = _by_prefix(params, "action")
        task_id = _by_prefix(params, "task_id")
        for ev in self._events:
            if (
                ev.entity_type == entity_type
                and ev.action == action
                and ev.task_id == task_id
            ):
                return _StubResult(ev)
        return _StubResult(None)

    async def commit(self) -> None:
        self.commit_calls += 1


class _StubResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


def _bound_params(stmt: Any) -> dict[str, Any]:
    try:
        return dict(stmt.compile().params)
    except Exception:
        return {}


def _by_prefix(params: dict[str, Any], name: str) -> Any:
    """SQLAlchemy 2.x ORM SELECTs suffix bound names (``task_id_1``)."""
    for k, v in params.items():
        if k == name or k.startswith(f"{name}_"):
            return v
    return None


class _StubDispatcher:
    """Mock the persistence seam; record the call args for assertion."""

    def __init__(self, session: _StubSession) -> None:
        self._session = session
        self.calls: list[dict[str, Any]] = []

    async def persist_and_publish(self, session, **kwargs) -> _StubEvent:
        self.calls.append(kwargs)
        event = _StubEvent(
            entity_type=kwargs["entity_type"],
            action=kwargs["action"],
            task_id=kwargs.get("task_id"),
            plan_id=kwargs.get("plan_id"),
            payload=kwargs.get("payload", {}),
        )
        # Add to the session's event log so subsequent SELECT-by-triple
        # sees the new row + the idempotency guard fires on a retry.
        self._session._events.append(event)
        return event


# ── Test app factory ──────────────────────────────────────────────────────


@pytest.fixture
def app_and_state() -> tuple[FastAPI, _StubSession, _StubDispatcher]:
    app = FastAPI()
    app.include_router(events_router)

    session = _StubSession()
    dispatcher = _StubDispatcher(session)

    async def _override_session():
        yield session

    def _override_dispatcher():
        return dispatcher

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_dispatcher] = _override_dispatcher
    return app, session, dispatcher


# ── POST /api/v1/events ──────────────────────────────────────────────────


class TestCreateEventHappyPath:
    def test_creates_row_with_201(
        self,
        app_and_state: tuple[FastAPI, _StubSession, _StubDispatcher],
    ) -> None:
        app, session, dispatcher = app_and_state
        task_id = session.seed_task()
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/events",
                json={
                    "entity_type": "github",
                    "action": "pr_merged",
                    "task_id": str(task_id),
                    "payload": {
                        "repo": "joeLepper/treadmill",
                        "pr_number": 271,
                        "merged_sha": "abc123",
                        "head_branch": "feat/x",
                    },
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["entity_type"] == "github"
        assert body["action"] == "pr_merged"
        assert body["task_id"] == str(task_id)
        assert body["payload"]["pr_number"] == 271
        assert uuid.UUID(body["id"])

        # Dispatcher seam was called with the right args.
        assert len(dispatcher.calls) == 1
        call = dispatcher.calls[0]
        assert call["entity_type"] == "github"
        assert call["action"] == "pr_merged"
        assert call["task_id"] == task_id
        assert call["payload"]["pr_number"] == 271

    def test_accepts_payload_without_task_id(
        self,
        app_and_state: tuple[FastAPI, _StubSession, _StubDispatcher],
    ) -> None:
        """task_id is optional — generic passthrough for plan / system
        events that aren't task-scoped."""
        app, _, _ = app_and_state
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/events",
                json={
                    "entity_type": "system",
                    "action": "ack",
                    "payload": {},
                },
            )
        assert resp.status_code == 201
        assert resp.json()["task_id"] is None


class TestCreateEvent404UnknownTask:
    def test_404_when_task_id_does_not_resolve(
        self,
        app_and_state: tuple[FastAPI, _StubSession, _StubDispatcher],
    ) -> None:
        app, _, dispatcher = app_and_state
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/events",
                json={
                    "entity_type": "github",
                    "action": "pr_merged",
                    "task_id": str(uuid.uuid4()),
                    "payload": {},
                },
            )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]
        # No dispatcher call on the failure path.
        assert dispatcher.calls == []


class TestCreateEvent409Duplicate:
    def test_409_on_duplicate_triple(
        self,
        app_and_state: tuple[FastAPI, _StubSession, _StubDispatcher],
    ) -> None:
        app, session, _ = app_and_state
        task_id = session.seed_task()
        session.seed_event(
            entity_type="github", action="pr_merged", task_id=task_id
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/events",
                json={
                    "entity_type": "github",
                    "action": "pr_merged",
                    "task_id": str(task_id),
                    "payload": {},
                },
            )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    def test_duplicate_triple_ignores_payload_contents(
        self,
        app_and_state: tuple[FastAPI, _StubSession, _StubDispatcher],
    ) -> None:
        """Path A (webhook) and Path B (manual fire) may legitimately
        carry slightly different payload shapes. The idempotency
        guard intentionally narrows on triple, not payload."""
        app, session, _ = app_and_state
        task_id = session.seed_task()
        session.seed_event(
            entity_type="github",
            action="pr_merged",
            task_id=task_id,
            payload={"webhook_specific": "field"},
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/events",
                json={
                    "entity_type": "github",
                    "action": "pr_merged",
                    "task_id": str(task_id),
                    "payload": {"manual_specific": "field"},
                },
            )
        assert resp.status_code == 409

    def test_same_action_different_task_id_is_not_duplicate(
        self,
        app_and_state: tuple[FastAPI, _StubSession, _StubDispatcher],
    ) -> None:
        app, session, _ = app_and_state
        task_a = session.seed_task()
        task_b = session.seed_task()
        session.seed_event(
            entity_type="github", action="pr_merged", task_id=task_a
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/events",
                json={
                    "entity_type": "github",
                    "action": "pr_merged",
                    "task_id": str(task_b),
                    "payload": {},
                },
            )
        assert resp.status_code == 201


class TestCreateEventGenericPassthrough:
    def test_accepts_arbitrary_entity_type_and_action(
        self,
        app_and_state: tuple[FastAPI, _StubSession, _StubDispatcher],
    ) -> None:
        app, session, _ = app_and_state
        task_id = session.seed_task()
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/events",
                json={
                    "entity_type": "deployment",
                    "action": "rolled_back",
                    "task_id": str(task_id),
                    "payload": {"image": "treadmill-api:abc"},
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["entity_type"] == "deployment"
        assert body["action"] == "rolled_back"
        assert body["payload"]["image"] == "treadmill-api:abc"


# ── dict-payload regression (Phase 5 deploy residual) ─────────────────


class _RecordingPublisher:
    """Real-shaped publisher: exercises _build_record/encode_payload."""

    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event, typed_payload) -> None:
        from treadmill_api.events.registry import encode_payload

        # The real SNS/logging publishers serialize through
        # encode_payload — do the same so a dict payload that would
        # crash them crashes this test.
        self.published.append(encode_payload(typed_payload))


def test_manual_event_with_real_dispatcher_dict_payload() -> None:
    """POST /api/v1/events passes a plain dict payload into the REAL
    ``Dispatcher.persist_and_publish`` (not a stub). Regression for the
    2026-06-10 coordinator outage: every prior test stubbed the
    dispatcher, so ``payload.model_dump`` blowing up on dict was
    invisible until the live coordinator exercised the route."""
    import uuid as _uuid
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from treadmill_api.dependencies_db import get_session
    from treadmill_api.dispatch import Dispatcher, get_dispatcher
    from treadmill_api.routers.events import router as events_router

    class _Session:
        def __init__(self) -> None:
            self.added = []

        def add(self, obj) -> None:
            self.added.append(obj)

        async def flush(self) -> None:
            for obj in self.added:
                if getattr(obj, "id", None) is None:
                    obj.id = _uuid.uuid4()
                if getattr(obj, "created_at", None) is None:
                    from datetime import datetime, timezone

                    obj.created_at = datetime.now(timezone.utc)

        async def commit(self) -> None:
            pass

        async def refresh(self, obj) -> None:
            pass

        async def execute(self, stmt):  # duplicate-triple probe
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            result.first.return_value = None
            return result

        async def get(self, model, pk):
            return None

    publisher = _RecordingPublisher()
    session = _Session()
    app = FastAPI()
    app.include_router(events_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_dispatcher] = lambda: Dispatcher(
        publisher=publisher
    )
    client = TestClient(app)

    resp = client.post(
        "/api/v1/events",
        json={
            "entity_type": "task",
            "action": "ci_result",
            "payload": {"check_name": "services/api", "conclusion": "success"},
        },
    )
    assert resp.status_code == 201, resp.text
    assert publisher.published == [
        {"check_name": "services/api", "conclusion": "success"}
    ]
