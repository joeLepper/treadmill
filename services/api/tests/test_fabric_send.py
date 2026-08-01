"""#235 — the imperative fabric message-send client + the #370 event-sink token-fix.

Mirrors ``test_fabric_event_sink.py``: HTTP is mocked with ``unittest.mock.AsyncMock``
(no respx/pytest-httpx in the repo). ``asyncio_mode = "auto"`` (pyproject) runs the async
tests without an explicit marker.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import httpx

from treadmill_api.coordination.fabric_send import FabricSend, make_fabric_send


def _client(status: int = 200) -> AsyncMock:
    c = AsyncMock()
    c.post = AsyncMock(return_value=Mock(status_code=status))
    c.aclose = AsyncMock()
    return c


def _configured(client: AsyncMock | None = None) -> FabricSend:
    return FabricSend(ingress_url="http://ingress:4500/events", token="tok", http_client=client)


# ── is_configured (dark unless BOTH url + token) ──────────────────────────────
def test_is_configured_needs_both_url_and_token():
    assert FabricSend(ingress_url="u", token="t").is_configured is True
    assert FabricSend(ingress_url="u", token=None).is_configured is False
    assert FabricSend(ingress_url=None, token="t").is_configured is False
    assert FabricSend(ingress_url="", token="t").is_configured is False


# ── valid_sender (namespace confinement mirror of the ingress \A..\z gate) ────
def test_valid_sender_accepts_treadmill_team_labels():
    for ok in ["coordinator-app", "worker-app-2", "evaluator-joelepper-treadmill"]:
        assert FabricSend.valid_sender(ok) is True


def test_valid_sender_rejects_siblings_reserved_and_malformed():
    for bad in [
        "potter", "operator", "ingress", "telegram",       # sibling / reserved
        "random-x", "coordinator", "admin-1",              # not a treadmill-team prefix
        "Coordinator-App", "worker_1", "coordinator-a.b",  # charset
        "coordinator-app\n", "coordinator-app\r",          # the trailing-newline slip (carla #235)
        "", None, 5,                                        # empty / non-str
    ]:
        assert FabricSend.valid_sender(bad) is False


# ── send() ────────────────────────────────────────────────────────────────────
async def test_send_valid_message_posts_and_returns_true():
    client = _client(200)
    fs = _configured(client)
    ok = await fs.send(to="worker-x-1", sender="coordinator-app", text="brief: take task 5")
    assert ok is True
    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args[0] == "http://ingress:4500/events"
    body = kwargs["json"]
    assert body["kind"] == "message"
    assert body["to"] == "worker-x-1"
    assert body["sender"] == "coordinator-app"
    assert body["text"] == "brief: take task 5"
    assert isinstance(body["ts"], int)  # the ingress freshness gate expects an integer


async def test_send_non_2xx_returns_false():
    fs = _configured(_client(401))
    assert await fs.send(to="worker-x-1", sender="coordinator-app", text="brief") is False


async def test_send_transport_error_returns_false():
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
    fs = _configured(client)
    assert await fs.send(to="worker-x-1", sender="coordinator-app", text="brief") is False


async def test_send_refuses_non_treadmill_sender_without_posting():
    client = _client(200)
    fs = _configured(client)
    assert await fs.send(to="worker-x-1", sender="potter", text="brief") is False
    client.post.assert_not_awaited()


async def test_send_refuses_empty_text_or_bad_to_without_posting():
    client = _client(200)
    fs = _configured(client)
    assert await fs.send(to="worker-x-1", sender="coordinator-app", text="") is False
    assert await fs.send(to="", sender="coordinator-app", text="brief") is False
    client.post.assert_not_awaited()


async def test_send_dark_when_unconfigured_returns_false():
    fs = FabricSend(ingress_url=None, token=None)
    assert await fs.send(to="worker-x-1", sender="coordinator-app", text="brief") is False


# ── start()/stop() client lifecycle + the bearer header ───────────────────────
async def test_start_builds_client_with_bearer_header_and_stop_closes_it():
    fs = FabricSend(ingress_url="http://ingress:4500/events", token="s3cret")
    await fs.start()
    try:
        assert fs._http_client is not None
        assert fs._http_client.headers["authorization"] == "Bearer s3cret"
    finally:
        await fs.stop()
    assert fs._http_client is None


async def test_start_is_noop_when_dark():
    fs = FabricSend(ingress_url=None, token=None)
    await fs.start()
    assert fs._http_client is None


def test_make_fabric_send_threads_url_and_token_from_settings():
    settings = Mock(fabric_ingress_url="http://ingress:4500/events", fabric_ingress_token="tok")
    fs = make_fabric_send(settings)
    assert fs.ingress_url == "http://ingress:4500/events"
    assert fs.is_configured is True


# ── #370 event-sink token-fix (folded into #235) ──────────────────────────────
def test_fabric_event_sink_token_threaded_from_settings():
    # #235 fold: the #370 event sink must authenticate to the #236 fail-closed ingress. Verify the
    # token threads through the constructor + factory (the Authorization header is built in start(),
    # which also subscribes to the eventbus — the identical bearer idiom is exercised by fabric_send).
    from treadmill_api.coordination.fabric_event_sink import (
        FabricEventSink,
        make_fabric_event_sink,
    )

    assert FabricEventSink(ingress_url="u", token="tok")._token == "tok"
    settings = Mock(fabric_ingress_url="u", fabric_ingress_token="tok")
    assert make_fabric_event_sink(settings)._token == "tok"
