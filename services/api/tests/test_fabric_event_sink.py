"""Unit tests for the exec_otp fabric event push sink (ADR-0087 fabric
migration).

Exercises the sink against a mocked ``httpx.AsyncClient`` and a stub
session factory so the coordinator resolution, POST body shape, dark-by-
default gating, and failure isolation are all observable without a real
HTTP server or database.

Coverage:

  * ``handle`` POSTs the ``{coordinator_label, event_type, payload, ts}``
    body when an event resolves to a coordinator (plan.submitted payload
    fast path).
  * The DB path resolves ``coordinator_label`` via the session factory
    for a non-plan.submitted event, and the POST carries that label.
  * With no ingress URL configured (dark default) NO POST fires and
    ``is_configured`` is False, so the lifespan ``start()`` short-circuit
    is observable.
  * An event that resolves to no coordinator is dropped (no POST).
  * A failing POST does not raise out of ``handle`` (failure isolation).
  * ``make_fabric_event_sink`` reads ``fabric_ingress_url`` off Settings.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from treadmill_api.coordination.fabric_event_sink import (
    FabricEventSink,
    _event_type,
    _resolve_coordinator_label,
    make_fabric_event_sink,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _plan_submitted_record(*, coordinator_label: str = "coordinator-ramjac") -> dict[str, Any]:
    """A ``_build_record``-shaped dict for ``plan.submitted`` — carries the
    coordinator_label in its payload (the pre-commit-safe fast path)."""
    plan_id = str(uuid.uuid4())
    return {
        "event_id": str(uuid.uuid4()),
        "entity_type": "plan",
        "action": "submitted",
        "task_id": None,
        "plan_id": plan_id,
        "run_id": None,
        "step_id": None,
        "payload": {
            "repo": "joeLepper/treadmill",
            "coordinator_label": coordinator_label,
            "task_count": 3,
        },
    }


def _task_record(*, action: str = "pr_merged") -> dict[str, Any]:
    """A ``_build_record``-shaped dict for a task event whose payload does
    NOT carry a coordinator_label — forces the DB resolution path."""
    return {
        "event_id": str(uuid.uuid4()),
        "entity_type": "task",
        "action": action,
        "task_id": str(uuid.uuid4()),
        "plan_id": str(uuid.uuid4()),
        "run_id": None,
        "step_id": None,
        "payload": {"repo": "joeLepper/treadmill", "pr_number": 42},
    }


def _mock_http_client() -> AsyncMock:
    """An ``AsyncMock`` shaped enough to stand in for ``httpx.AsyncClient``."""
    client = AsyncMock()
    client.post = AsyncMock()
    client.aclose = AsyncMock()
    return client


class _StubResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _StubSession:
    """Async-context-manager session returning a fixed coordinator_label row."""

    def __init__(self, label: str | None) -> None:
        self._label = label

    async def __aenter__(self) -> "_StubSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, *_args: Any, **_kwargs: Any) -> _StubResult:
        return _StubResult((self._label,) if self._label is not None else None)


def _stub_session_factory(label: str | None) -> Any:
    def factory() -> _StubSession:
        return _StubSession(label)

    return factory


# ── event_type composition ────────────────────────────────────────────────────


def test_event_type_composes_entity_and_action() -> None:
    assert _event_type(_plan_submitted_record()) == "plan.submitted"
    assert _event_type(_task_record(action="pr_merged")) == "task.pr_merged"
    assert (
        _event_type(
            {"entity_type": "check_run", "action": "completed"}
        )
        == "check_run.completed"
    )


# ── coordinator resolution ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_uses_plan_submitted_payload_fast_path() -> None:
    """plan.submitted carries coordinator_label in payload; no DB needed."""
    record = _plan_submitted_record(coordinator_label="coordinator-alpha")
    # Session factory is present but must NOT be consulted for the fast path.
    label = await _resolve_coordinator_label(
        record, _stub_session_factory("wrong-label"),
    )
    assert label == "coordinator-alpha"


@pytest.mark.asyncio
async def test_resolve_payload_fast_path_gated_to_plan_submitted() -> None:
    """A non-plan.submitted event whose payload carries coordinator_label
    must NOT match the payload fast path — it falls through to the DB
    (mirrors the WS security gate against forged manual-event payloads)."""
    record = _task_record()
    record["payload"]["coordinator_label"] = "forged-label"
    label = await _resolve_coordinator_label(
        record, _stub_session_factory("db-resolved-label"),
    )
    assert label == "db-resolved-label"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_row_and_no_factory() -> None:
    record = _task_record()
    assert await _resolve_coordinator_label(record, None) is None
    assert await _resolve_coordinator_label(record, _stub_session_factory(None)) is None


# ── handle: POST shape + gating ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_posts_expected_body_on_plan_submitted() -> None:
    client = _mock_http_client()
    sink = FabricEventSink(
        ingress_url="http://fabric-core:4500/events",
        session_factory=None,
        http_client=client,
    )
    record = _plan_submitted_record(coordinator_label="coordinator-ramjac")

    await sink.handle(record)

    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args[0] == "http://fabric-core:4500/events"
    body = kwargs["json"]
    assert body["coordinator_label"] == "coordinator-ramjac"
    assert body["event_type"] == "plan.submitted"
    assert body["payload"] == record["payload"]
    # ts is an offset-aware ISO-8601 UTC timestamp.
    parsed = datetime.fromisoformat(body["ts"])
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_handle_posts_with_db_resolved_label() -> None:
    client = _mock_http_client()
    sink = FabricEventSink(
        ingress_url="http://fabric-core:4500/events",
        session_factory=_stub_session_factory("coordinator-beta"),
        http_client=client,
    )
    record = _task_record(action="pr_merged")

    await sink.handle(record)

    client.post.assert_awaited_once()
    body = client.post.call_args.kwargs["json"]
    assert body["coordinator_label"] == "coordinator-beta"
    assert body["event_type"] == "task.pr_merged"


@pytest.mark.asyncio
async def test_handle_drops_event_with_no_coordinator() -> None:
    """An event that resolves to no coordinator is not POSTed — the fabric
    routes by coordinator_label, so it has no inbox to land in."""
    client = _mock_http_client()
    sink = FabricEventSink(
        ingress_url="http://fabric-core:4500/events",
        session_factory=_stub_session_factory(None),
        http_client=client,
    )

    await sink.handle(_task_record())

    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_no_post_when_dark() -> None:
    """Dark by default: no ingress URL => no POST, is_configured False."""
    client = _mock_http_client()
    sink = FabricEventSink(
        ingress_url=None,
        session_factory=_stub_session_factory("coordinator-gamma"),
        http_client=client,
    )
    assert sink.is_configured is False

    await sink.handle(_plan_submitted_record())

    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_string_url_is_dark() -> None:
    """FABRIC_INGRESS_URL="" behaves exactly like unset."""
    sink = FabricEventSink(ingress_url="")
    assert sink.ingress_url is None
    assert sink.is_configured is False


# ── failure isolation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_swallows_post_failure() -> None:
    """A failing POST logs + continues; handle never raises into the loop."""
    client = _mock_http_client()
    client.post.side_effect = RuntimeError("ingress unreachable")
    sink = FabricEventSink(
        ingress_url="http://fabric-core:4500/events",
        session_factory=None,
        http_client=client,
    )

    # Must not raise.
    await sink.handle(_plan_submitted_record())
    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_swallows_lookup_failure() -> None:
    """A DB lookup error drops the event without raising or POSTing."""
    client = _mock_http_client()

    def exploding_factory() -> Any:
        raise RuntimeError("db down")

    sink = FabricEventSink(
        ingress_url="http://fabric-core:4500/events",
        session_factory=exploding_factory,
        http_client=client,
    )

    await sink.handle(_task_record())
    client.post.assert_not_awaited()


# ── start()/stop() lifecycle ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_is_noop_when_dark() -> None:
    """A dark sink's start() must not subscribe or spawn a task."""
    sink = FabricEventSink(ingress_url=None)
    await sink.start()
    assert sink._task is None
    assert sink._queue is None
    # stop() is safe on a never-started instance.
    await sink.stop()


@pytest.mark.asyncio
async def test_start_stop_roundtrip_when_configured() -> None:
    client = _mock_http_client()
    sink = FabricEventSink(
        ingress_url="http://fabric-core:4500/events",
        http_client=client,
    )
    await sink.start()
    assert sink._task is not None
    assert sink._queue is not None
    await sink.stop()
    assert sink._task is None
    # Injected client is NOT closed by the sink.
    client.aclose.assert_not_awaited()


# ── make_fabric_event_sink from Settings ───────────────────────────────────────


def test_make_fabric_event_sink_reads_settings() -> None:
    class _S:
        fabric_ingress_url = "http://fabric-core:4500/events"

    sink = make_fabric_event_sink(_S(), session_factory="sf")
    assert sink.ingress_url == "http://fabric-core:4500/events"
    assert sink._session_factory == "sf"
    assert sink.is_configured is True
