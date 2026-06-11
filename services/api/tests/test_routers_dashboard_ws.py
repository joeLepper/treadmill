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


# ── ADR-0084: coordinator plan_ids subscription ────────────────────────────────


def test_plan_ids_filter_forwards_matching_plan_id(monkeypatch) -> None:
    """``?plan_ids=<uuid>`` forwards events whose plan_id is in the set
    without consulting ``_lookup_created_by`` — the coordinator path bypasses
    the per-event DB lookup that the created_by filter requires.
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_in = str(uuid.uuid4())
    plan_out = str(uuid.uuid4())

    async def stub_lookup(plan_id, task_id, session_factory=None):
        raise AssertionError("plan_ids path must not hit _lookup_created_by")

    monkeypatch.setattr(ws_module, "_lookup_created_by", stub_lookup)

    app = _build_app()
    rec_in = _make_record(plan_id=plan_in)
    rec_out = _make_record(plan_id=plan_out)
    rec_in_sentinel = _make_record(plan_id=plan_in)

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/v1/dashboard/ws/events?plan_ids={plan_in}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            _broadcast_local(rec_in)
            _broadcast_local(rec_out)
            _broadcast_local(rec_in_sentinel)

            frame1 = ws.receive_json()
            frame2 = ws.receive_json()

    assert frame1["plan_id"] == plan_in
    assert frame2["plan_id"] == plan_in


def test_plan_ids_composes_with_created_by_by_or(monkeypatch) -> None:
    """When both ``created_by`` and ``plan_ids`` are set, the filters
    compose by OR: a frame is forwarded if EITHER matches.
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_coord = str(uuid.uuid4())  # subscribed via plan_ids
    plan_label = str(uuid.uuid4())  # subscribed via created_by
    plan_neither = str(uuid.uuid4())  # neither — drop

    async def stub_lookup(plan_id, task_id, session_factory=None):
        if plan_id == plan_label:
            return "lbl-a"
        return "other-label"

    monkeypatch.setattr(ws_module, "_lookup_created_by", stub_lookup)

    app = _build_app()
    rec_coord = _make_record(plan_id=plan_coord)
    rec_label = _make_record(plan_id=plan_label)
    rec_neither = _make_record(plan_id=plan_neither)
    rec_sentinel = _make_record(plan_id=plan_label)

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/v1/dashboard/ws/events?created_by=lbl-a&plan_ids={plan_coord}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            _broadcast_local(rec_coord)
            _broadcast_local(rec_label)
            _broadcast_local(rec_neither)
            _broadcast_local(rec_sentinel)

            frame1 = ws.receive_json()
            frame2 = ws.receive_json()
            frame3 = ws.receive_json()

    plan_ids_received = {frame1["plan_id"], frame2["plan_id"], frame3["plan_id"]}
    assert plan_ids_received == {plan_coord, plan_label}


def test_plan_ids_filter_drops_non_matching_plan_id() -> None:
    """A non-matching plan_id with no ``created_by`` set drops silently.

    Sentinel pattern: publish a matching event last so we observe exactly
    one frame and infer the non-matching one was dropped.
    """
    plan_in = str(uuid.uuid4())
    plan_out = str(uuid.uuid4())

    app = _build_app()
    rec_out = _make_record(plan_id=plan_out)
    rec_in_sentinel = _make_record(plan_id=plan_in)

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/v1/dashboard/ws/events?plan_ids={plan_in}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            _broadcast_local(rec_out)
            _broadcast_local(rec_in_sentinel)

            frame = ws.receive_json()

    assert frame["plan_id"] == plan_in


def test_plan_ids_filter_normalises_case_and_skips_malformed() -> None:
    """Mixed-case UUIDs match canonical lower-case event plan_ids, and a
    malformed entry in the list is silently skipped without breaking the
    rest of the subscription.
    """
    plan_real = str(uuid.uuid4())  # canonical lowercase
    plan_real_upper = plan_real.upper()  # operator passes uppercase

    app = _build_app()
    rec_real = _make_record(plan_id=plan_real)

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/v1/dashboard/ws/events?plan_ids=not-a-uuid,{plan_real_upper}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            _broadcast_local(rec_real)
            frame = ws.receive_json()

    assert frame["plan_id"] == plan_real


def test_plan_ids_filter_drops_ownerless_events() -> None:
    """Ownerless events (no plan_id, no task_id) drop on any filtered
    connection — same behaviour as the created_by filter.
    """
    plan_in = str(uuid.uuid4())

    app = _build_app()
    rec_ownerless = _make_record()  # no plan_id, no task_id
    rec_sentinel = _make_record(plan_id=plan_in)

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/v1/dashboard/ws/events?plan_ids={plan_in}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            _broadcast_local(rec_ownerless)
            _broadcast_local(rec_sentinel)
            frame = ws.receive_json()

    assert frame["plan_id"] == plan_in


# ── ADR-0085+0086: coordinator_label subscription ──────────────────────────────


def test_coordinator_label_forwards_when_plan_repo_matches(monkeypatch) -> None:
    """``?coordinator_label=coordinator-medicoder`` forwards a
    plan.submitted event whose plan belongs to a repo whose team_config
    points at this coordinator. Closes the ADR-0085+0086 in-session
    pickup gap: new plans have created_by=<orchestrator> so the
    plain created_by filter never matches; resolving plan → repo →
    team_configs.coordinator_label is the path that does.
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_owned = str(uuid.uuid4())     # belongs to coordinator-medicoder
    plan_other = str(uuid.uuid4())     # belongs to coordinator-otherrepo
    plan_unknown = str(uuid.uuid4())   # no team_config row

    async def stub_coord_lookup(plan_id, session_factory=None):
        if plan_id == plan_owned:
            return "coordinator-medicoder"
        if plan_id == plan_other:
            return "coordinator-otherrepo"
        return None

    monkeypatch.setattr(
        ws_module, "_lookup_coordinator_label", stub_coord_lookup
    )

    app = _build_app()
    rec_other = _make_record(plan_id=plan_other)        # non-matching coord
    rec_unknown = _make_record(plan_id=plan_unknown)    # unmatched plan
    rec_owned = _make_record(plan_id=plan_owned)        # matching
    rec_sentinel = _make_record(plan_id=plan_owned)     # sentinel

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events"
            "?coordinator_label=coordinator-medicoder"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            _broadcast_local(rec_other)
            _broadcast_local(rec_unknown)
            _broadcast_local(rec_owned)
            _broadcast_local(rec_sentinel)

            frame1 = ws.receive_json()
            frame2 = ws.receive_json()

    assert frame1["type"] == "event"
    assert frame1["plan_id"] == plan_owned
    assert frame2["type"] == "event"
    assert frame2["plan_id"] == plan_owned


def test_coordinator_label_composes_with_created_by_and_plan_ids_by_or(
    monkeypatch,
) -> None:
    """All three filters compose by OR. A frame is forwarded if ANY of
    plan_ids / coordinator_label / created_by matches; only the
    none-matching frame is dropped.
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_via_plan_ids = str(uuid.uuid4())
    plan_via_coord_label = str(uuid.uuid4())
    plan_via_created_by = str(uuid.uuid4())
    plan_neither = str(uuid.uuid4())

    async def stub_created_by(plan_id, task_id, session_factory=None):
        if plan_id == plan_via_created_by:
            return "lbl-a"
        return "other-label"

    async def stub_coord(plan_id, session_factory=None):
        if plan_id == plan_via_coord_label:
            return "coordinator-medicoder"
        return None

    monkeypatch.setattr(ws_module, "_lookup_created_by", stub_created_by)
    monkeypatch.setattr(ws_module, "_lookup_coordinator_label", stub_coord)

    app = _build_app()
    rec_plan_ids = _make_record(plan_id=plan_via_plan_ids)
    rec_coord = _make_record(plan_id=plan_via_coord_label)
    rec_label = _make_record(plan_id=plan_via_created_by)
    rec_neither = _make_record(plan_id=plan_neither)
    rec_sentinel = _make_record(plan_id=plan_via_plan_ids)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events"
            "?created_by=lbl-a"
            f"&plan_ids={plan_via_plan_ids}"
            "&coordinator_label=coordinator-medicoder"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"

            _broadcast_local(rec_plan_ids)
            _broadcast_local(rec_coord)
            _broadcast_local(rec_label)
            _broadcast_local(rec_neither)
            _broadcast_local(rec_sentinel)

            frame1 = ws.receive_json()
            frame2 = ws.receive_json()
            frame3 = ws.receive_json()
            frame4 = ws.receive_json()

    plan_ids_received = {
        frame1["plan_id"], frame2["plan_id"],
        frame3["plan_id"], frame4["plan_id"],
    }
    assert plan_neither not in plan_ids_received
    assert plan_ids_received == {
        plan_via_plan_ids, plan_via_coord_label, plan_via_created_by
    }


def test_coordinator_label_lookup_is_cached(monkeypatch) -> None:
    """Multiple events on the same plan_id trigger exactly one
    ``_lookup_coordinator_label`` call — the per-connection cache absorbs
    the hot path. Mirrors the equivalent cached-lookup contract for
    ``_lookup_created_by``.
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_id = str(uuid.uuid4())
    call_count = 0

    async def stub_lookup(pid, session_factory=None):
        nonlocal call_count
        call_count += 1
        return "coordinator-medicoder"

    monkeypatch.setattr(ws_module, "_lookup_coordinator_label", stub_lookup)

    app = _build_app()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events"
            "?coordinator_label=coordinator-medicoder"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            for _ in range(5):
                _broadcast_local(_make_record(plan_id=plan_id))
            for _ in range(5):
                ws.receive_json()

    assert call_count == 1


def test_coordinator_label_lookup_failure_drops_event(monkeypatch) -> None:
    """A ``_lookup_coordinator_label`` exception drops the event but
    keeps the socket alive. Mirrors the created_by failure-path contract.
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_failing = str(uuid.uuid4())
    plan_ok = str(uuid.uuid4())

    async def stub_lookup(pid, session_factory=None):
        if pid == plan_failing:
            raise RuntimeError("simulated DB blip")
        return "coordinator-medicoder"

    monkeypatch.setattr(ws_module, "_lookup_coordinator_label", stub_lookup)

    app = _build_app()
    rec_fail = _make_record(plan_id=plan_failing)
    rec_sentinel = _make_record(plan_id=plan_ok)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events"
            "?coordinator_label=coordinator-medicoder"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            _broadcast_local(rec_fail)
            _broadcast_local(rec_sentinel)
            frame = ws.receive_json()

    # The failing-lookup event was dropped; the socket survived and
    # delivered the next event.
    assert frame["plan_id"] == plan_ok


# ── task 9b7c1286: plan.submitted pickup loss (publish-before-commit race) ─────


def test_plan_submitted_matches_payload_coordinator_label_without_db(
    monkeypatch,
) -> None:
    """``plan.submitted`` carries ``coordinator_label`` in its payload; the
    filter must match on it directly, BEFORE any DB lookup.

    This is the regression test for the live pickup loss: the event is
    broadcast in-process from inside the still-open ``POST /plans``
    transaction, so a plan → repo → team_configs lookup from the WS
    handler's separate session cannot see the plan row yet and returns
    None — dropping the one event the coordinator_label branch exists to
    deliver. The stub raises to prove the payload path never consults
    the DB.
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    async def stub_lookup(plan_id, session_factory=None):
        raise AssertionError(
            "payload coordinator_label match must not hit the DB"
        )

    monkeypatch.setattr(ws_module, "_lookup_coordinator_label", stub_lookup)

    plan_new = str(uuid.uuid4())
    rec = _make_record(
        plan_id=plan_new, entity_type="plan", action="submitted",
    )
    rec["payload"] = {
        "repo": "joeLepper/treadmill",
        "coordinator_label": "coordinator-treadmill",
        "task_count": 3,
    }

    with TestClient(_build_app()) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events"
            "?coordinator_label=coordinator-treadmill"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            _broadcast_local(rec)
            frame = ws.receive_json()

    assert frame["type"] == "event"
    assert frame["entity_type"] == "plan"
    assert frame["action"] == "submitted"
    assert frame["plan_id"] == plan_new


def test_payload_coordinator_label_mismatch_still_falls_through(
    monkeypatch,
) -> None:
    """A payload naming a DIFFERENT coordinator does not match; the DB
    path still runs and can match on its own merits."""
    from treadmill_api.routers.dashboard import ws as ws_module

    plan_id = str(uuid.uuid4())

    async def stub_lookup(pid, session_factory=None):
        return "coordinator-mine"

    monkeypatch.setattr(ws_module, "_lookup_coordinator_label", stub_lookup)

    rec = _make_record(plan_id=plan_id, entity_type="plan", action="submitted")
    rec["payload"] = {"coordinator_label": "coordinator-other"}

    with TestClient(_build_app()) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events?coordinator_label=coordinator-mine"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            _broadcast_local(rec)
            frame = ws.receive_json()

    # Delivered via the DB branch, not the (mismatching) payload branch.
    assert frame["plan_id"] == plan_id


def test_negative_created_by_lookup_is_not_cached_forever(monkeypatch) -> None:
    """A ``None`` owner resolution must not permanently blind the socket:
    once the negative TTL lapses, the next event for the same plan
    re-resolves and is delivered. With the TTL shrunk to 0 the second
    lookup happens immediately.

    Guards the second half of the pickup-loss bug: the old permanent
    ``_owner_cache[key] = None`` meant a single lookup racing the
    emitter's commit silenced that plan for the socket's lifetime.
    """
    from treadmill_api.routers.dashboard import ws as ws_module

    monkeypatch.setattr(ws_module, "_NEGATIVE_TTL_S", 0.0)

    plan_id = str(uuid.uuid4())
    results = iter([None, "lbl-a"])  # first lookup races the commit
    call_count = 0

    async def stub_lookup(pid, task_id, session_factory=None):
        nonlocal call_count
        call_count += 1
        return next(results)

    monkeypatch.setattr(ws_module, "_lookup_created_by", stub_lookup)

    rec1 = _make_record(plan_id=plan_id)
    rec2 = _make_record(plan_id=plan_id)

    with TestClient(_build_app()) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events?created_by=lbl-a"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            _broadcast_local(rec1)  # resolves None → dropped
            _broadcast_local(rec2)  # re-resolves → delivered
            frame = ws.receive_json()

    assert frame["plan_id"] == plan_id
    assert call_count == 2


def test_negative_coordinator_lookup_is_not_cached_forever(monkeypatch) -> None:
    """Same negative-TTL contract for the ``coordinator_label`` DB path."""
    from treadmill_api.routers.dashboard import ws as ws_module

    monkeypatch.setattr(ws_module, "_NEGATIVE_TTL_S", 0.0)

    plan_id = str(uuid.uuid4())
    results = iter([None, "coordinator-mine"])

    async def stub_lookup(pid, session_factory=None):
        return next(results)

    monkeypatch.setattr(ws_module, "_lookup_coordinator_label", stub_lookup)

    rec1 = _make_record(plan_id=plan_id)
    rec2 = _make_record(plan_id=plan_id)

    with TestClient(_build_app()) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events?coordinator_label=coordinator-mine"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            _broadcast_local(rec1)
            _broadcast_local(rec2)
            frame = ws.receive_json()

    assert frame["plan_id"] == plan_id


def test_negative_lookup_within_ttl_uses_cache(monkeypatch) -> None:
    """Inside the negative TTL the cache absorbs repeat lookups — the
    per-event DB cost on a busy unfiltered feed stays bounded."""
    from treadmill_api.routers.dashboard import ws as ws_module

    # Default TTL (30s) is far longer than the test run.
    plan_id = str(uuid.uuid4())
    plan_sentinel = str(uuid.uuid4())
    call_count = 0

    async def stub_lookup(pid, task_id, session_factory=None):
        nonlocal call_count
        call_count += 1
        return "lbl-a" if pid == plan_sentinel else None

    monkeypatch.setattr(ws_module, "_lookup_created_by", stub_lookup)

    with TestClient(_build_app()) as client:
        with client.websocket_connect(
            "/api/v1/dashboard/ws/events?created_by=lbl-a"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            for _ in range(5):
                _broadcast_local(_make_record(plan_id=plan_id))
            _broadcast_local(_make_record(plan_id=plan_sentinel))
            frame = ws.receive_json()

    assert frame["plan_id"] == plan_sentinel
    # 1 negative resolution (cached for the next 4) + 1 for the sentinel.
    assert call_count == 2
