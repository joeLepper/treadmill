"""Unit tests for ``WS /api/v1/dashboard/ws/events`` (ADR-0056, dashboard B).

Exercises the WebSocket router through Starlette's TestClient — no
network, no live database. We hand the route the publisher path it
expects (via ``LoggingEventPublisher.publish``, which fans out to
in-process subscribers as a side effect) and assert frames land on the
socket in the right shape and cadence.

Coverage:

  * ``hello`` frame on connect.
  * A published event lands on the socket as ``{type: event, ...}`` with
    the projected shape the dashboard consumes (including ``plan_id``).
  * ``heartbeat`` frames fire periodically (we shrink the interval via
    the route's ``heartbeat_interval`` query param).
  * Disconnecting the client tears down cleanly without raising into
    the router runtime.
  * ``?created_by=<label>`` filter: matching events arrive, non-matching
    and ownerless events are dropped.
  * Owner resolution is cached per-connection: two events for the same
    plan_id trigger exactly one ``_lookup_created_by`` call.
  * A lookup exception drops the event but leaves the socket alive.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.eventbus import (
    LoggingEventPublisher,
    _build_record,
    _broadcast_local,
)
from treadmill_api.events import TaskRegistered
from treadmill_api.models import Event
from treadmill_api.routers.dashboard import router as dashboard_router


# ── helpers ────────────────────────────────────────────────────────────────────


def _build_app() -> FastAPI:
    """A bare FastAPI app with the dashboard aggregator mounted.

    The WS endpoint is auto-discovered (sibling ``ws.py`` exporting
    ``router``), so no per-test wiring is needed — the same mount the
    real ``app.py`` uses backs the test.
    """
    app = FastAPI()
    app.include_router(dashboard_router)
    return app


def _make_event(*, task_id: uuid.UUID | None = None) -> Event:
    """In-memory Event row sufficient for ``EventPublisher.publish``."""
    return Event(
        id=uuid.uuid4(),
        entity_type="task",
        action="registered",
        task_id=task_id or uuid.uuid4(),
        plan_id=None,
        run_id=None,
        step_id=None,
        payload={},
        created_at=datetime.now(timezone.utc),
    )


def _valid_payload() -> TaskRegistered:
    return TaskRegistered(
        repo="RAMJAC/treadmill",
        title="Add /health endpoint",
        workflow_version_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
    )


def _make_record(
    *,
    plan_id: str | None = None,
    task_id: str | None = None,
    entity_type: str = "task",
    action: str = "registered",
) -> dict:
    """Directly construct a broadcast record dict without hitting the DB.

    Used by filter tests so we can control plan_id / task_id precisely
    without building full Event + EventPayload objects.
    """
    return {
        "event_id": str(uuid.uuid4()),
        "entity_type": entity_type,
        "action": action,
        "task_id": task_id,
        "plan_id": plan_id,
        "run_id": None,
        "step_id": None,
        "payload": "{}",
    }


# ── tests ──────────────────────────────────────────────────────────────────────


def test_ws_route_is_auto_discovered() -> None:
    """``ws.py`` lands under the aggregator without an ``__init__.py``
    edit — the auto-discovery contract PRs in this package depend on."""
    from treadmill_api.routers.dashboard import MOUNTED_MODULES

    assert "ws" in MOUNTED_MODULES


def test_hello_frame_is_sent_on_connect() -> None:
    """The first frame after ``accept`` is the ``hello`` greeting so the
    client can flip the freshness affordance to ``ws`` immediately."""
    app = _build_app()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events"
        ) as ws:
            frame = ws.receive_json()
            assert frame["type"] == "hello"
            assert isinstance(frame["ts"], str) and frame["ts"]


def test_published_event_lands_on_socket() -> None:
    """An event row INSERT (modeled here as the publisher's record being
    handed to the in-process broadcaster) lands on the socket as the
    projected ``event`` frame the dashboard consumes, including the new
    ``plan_id`` field (ADR-0068).

    We invoke the broadcaster directly instead of awaiting
    ``LoggingEventPublisher.publish`` because Starlette's ``TestClient``
    runs the WS handler in a portal thread; calling an async publisher
    from the test thread would cross event loops to wake the handler's
    queue. The broadcaster is the seam the publisher fans through, so
    pushing a record into it exercises the same code the handler sees
    in production.
    """
    app = _build_app()
    event = _make_event()
    record = _build_record(event, _valid_payload())

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            _broadcast_local(record)

            frame = ws.receive_json()
            assert frame == {
                "type": "event",
                "id": str(event.id),
                "entity_type": "task",
                "action": "registered",
                "task_id": str(event.task_id),
                "plan_id": None,
                "ts": frame["ts"],
            }
            assert frame["ts"]


def test_logging_publisher_fans_out_to_in_process_subscribers() -> None:
    """End-to-end check on the eventbus-side change: a published event
    reaches every in-process subscriber via ``_broadcast_local`` — the
    seam the WS handler relies on. Kept separate from the socket test
    so the broadcast contract is asserted independently."""
    import asyncio

    publisher = LoggingEventPublisher()
    event = _make_event()
    payload = _valid_payload()

    async def _exercise() -> dict[str, str | None]:
        from treadmill_api.eventbus import subscribe_local, unsubscribe_local

        queue = subscribe_local()
        try:
            await publisher.publish(event, payload)
            return await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            unsubscribe_local(queue)

    record = asyncio.run(_exercise())
    assert record["event_id"] == str(event.id)
    assert record["entity_type"] == "task"
    assert record["action"] == "registered"
    assert record["task_id"] == str(event.task_id)


def test_heartbeat_fires_when_no_events() -> None:
    """With no event traffic, the handler still emits a heartbeat at the
    configured cadence so clients can detect dead sockets fast. Pass a
    very short ``heartbeat_interval`` so the test doesn't wait 25 s."""
    app = _build_app()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events?heartbeat_interval=0.1"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            start = time.monotonic()
            frame = ws.receive_json()
            elapsed = time.monotonic() - start

            assert frame["type"] == "heartbeat"
            assert isinstance(frame["ts"], str) and frame["ts"]
            # Fired soon after the interval — generous upper bound to
            # tolerate scheduler jitter on busy CI.
            assert elapsed < 2.0


def test_disconnect_does_not_crash_endpoint() -> None:
    """Closing the socket mid-loop must tear down cleanly: the handler
    catches the disconnect, unsubscribes, and returns normally so the
    router stays healthy for the next connection."""
    app = _build_app()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            # Closing here triggers the receive task's
            # WebSocketDisconnect; the handler's loop must honor it.

        # A second connection succeeds — proof the router survived the
        # previous teardown without leaking state.
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events"
        ) as ws2:
            assert ws2.receive_json()["type"] == "hello"


def test_created_by_filter_passes_matching_drops_others(monkeypatch) -> None:
    """``?created_by=lbl-a`` forwards the matching event and silently
    drops a non-matching event and an ownerless event (ADR-0068).

    Sentinel approach: after the three events we publish a second
    matching event as a sentinel so we can read exactly two event frames
    and assert both carry the expected plan_id — without relying on
    timing to prove the dropped events never arrived.
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_a = str(uuid.uuid4())
    plan_b = str(uuid.uuid4())

    async def stub_lookup(plan_id, task_id, session_factory=None):
        if plan_id == plan_a:
            return "lbl-a"
        if plan_id == plan_b:
            return "lbl-b"
        return None

    monkeypatch.setattr(ws_module, "_lookup_created_by", stub_lookup)

    app = _build_app()
    rec_b = _make_record(plan_id=plan_b)          # non-matching (lbl-b)
    rec_ownerless = _make_record()                  # no plan_id, no task_id
    rec_a = _make_record(plan_id=plan_a)           # matching (lbl-a)
    rec_sentinel = _make_record(plan_id=plan_a)    # second matching (sentinel)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events?created_by=lbl-a"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            _broadcast_local(rec_b)
            _broadcast_local(rec_ownerless)
            _broadcast_local(rec_a)
            _broadcast_local(rec_sentinel)

            frame1 = ws.receive_json()
            frame2 = ws.receive_json()

    assert frame1["type"] == "event"
    assert frame1["plan_id"] == plan_a
    assert frame2["type"] == "event"
    assert frame2["plan_id"] == plan_a


def test_created_by_lookup_is_cached(monkeypatch) -> None:
    """Two events for the same plan_id trigger exactly one
    ``_lookup_created_by`` call — the per-connection cache absorbs the
    second (ADR-0068).
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_a = str(uuid.uuid4())
    call_count = 0

    async def stub_lookup(plan_id, task_id, session_factory=None):
        nonlocal call_count
        call_count += 1
        return "lbl-a" if plan_id == plan_a else None

    monkeypatch.setattr(ws_module, "_lookup_created_by", stub_lookup)

    app = _build_app()
    rec1 = _make_record(plan_id=plan_a)
    rec2 = _make_record(plan_id=plan_a)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events?created_by=lbl-a"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            _broadcast_local(rec1)
            _broadcast_local(rec2)

            frame1 = ws.receive_json()
            frame2 = ws.receive_json()

    assert frame1["type"] == "event"
    assert frame2["type"] == "event"
    assert call_count == 1  # second event hit the cache


def test_lookup_exception_drops_event_socket_survives(monkeypatch) -> None:
    """A ``_lookup_created_by`` exception drops the event and logs, but
    the socket stays alive — a subsequent matching event still arrives
    (ADR-0068 belt-and-braces requirement).
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_bad = str(uuid.uuid4())
    plan_good = str(uuid.uuid4())

    async def stub_lookup(plan_id, task_id, session_factory=None):
        if plan_id == plan_bad:
            raise RuntimeError("simulated DB failure")
        return "lbl-a"

    monkeypatch.setattr(ws_module, "_lookup_created_by", stub_lookup)

    app = _build_app()
    rec_bad = _make_record(plan_id=plan_bad)    # lookup raises → drop
    rec_good = _make_record(plan_id=plan_good)  # lookup succeeds → send

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events?created_by=lbl-a"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            _broadcast_local(rec_bad)
            _broadcast_local(rec_good)

            frame = ws.receive_json()

    assert frame["type"] == "event"
    assert frame["plan_id"] == plan_good
