"""Unit tests for the escalation notification fan-out (ADR-0062 Step 4).

Exercises the subscriber against a mocked ``httpx.AsyncClient`` so the
gating logic, Slack-body shape, raw-webhook fan-out, and one-bad-target
failure isolation are all observable without a real HTTP server.

Coverage:

  * ``handle`` filters non-escalation events (no POST fires).
  * Slack POST body matches the documented shape on both open + close
    events (emoji, short task id, reason, MTTR-on-close).
  * Each configured raw-webhook URL receives a POST with the raw
    event record.
  * A failing target — Slack or one of the raw webhooks — does not
    block delivery to the remaining targets.
  * ``make_notification_fanout`` reads both env-driven fields off
    ``Settings``.
  * ``is_configured`` correctly reports the empty-target build so the
    lifespan handler's ``start()`` short-circuit is observable.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.notification_fanout import (
    NotificationFanout,
    _format_slack_body,
    make_notification_fanout,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _open_record(*, task_id: str | None = None, reason: str = "architect_cap") -> dict[str, Any]:
    """A ``_build_record``-shaped dict for ``task.escalated_to_operator``."""
    return {
        "event_id": str(uuid.uuid4()),
        "entity_type": "task",
        "action": "escalated_to_operator",
        "task_id": task_id or str(uuid.uuid4()),
        "plan_id": str(uuid.uuid4()),
        "run_id": None,
        "step_id": None,
        "payload": {
            "task_id": task_id or str(uuid.uuid4()),
            "repo": "joeLepper/treadmill",
            "reason": reason,
        },
    }


def _close_record(
    *, task_id: str | None = None,
    close_reason: str = "re_progressed",
    mttr_seconds: int = 1234,
) -> dict[str, Any]:
    """A ``_build_record``-shaped dict for ``task.escalation_closed``."""
    return {
        "event_id": str(uuid.uuid4()),
        "entity_type": "task",
        "action": "escalation_closed",
        "task_id": task_id or str(uuid.uuid4()),
        "plan_id": str(uuid.uuid4()),
        "run_id": None,
        "step_id": None,
        "payload": {
            "close_reason": close_reason,
            "opened_at": "2026-06-02T00:00:00+00:00",
            "mttr_seconds": mttr_seconds,
        },
    }


def _make_fanout(
    *,
    slack: str | None = None,
    raw: list[str] | None = None,
    http_client: Any | None = None,
) -> NotificationFanout:
    """Build a fanout for unit tests with an injected mock client.

    Passing an injected client also flips the close-on-stop behavior off
    so tests can introspect the same mock after stop().
    """
    return NotificationFanout(
        slack_webhook_url=slack,
        raw_webhook_urls=raw,
        http_client=http_client,
    )


def _mock_http_client() -> AsyncMock:
    """An ``AsyncMock`` shaped enough to stand in for ``httpx.AsyncClient``."""
    client = AsyncMock()
    client.post = AsyncMock()
    client.aclose = AsyncMock()
    return client


# ── Format helpers ────────────────────────────────────────────────────────────


def test_slack_open_body_contains_emoji_short_id_and_reason() -> None:
    """ADR-0062 Step 4 brief: Slack body carries emoji + task id snippet +
    reason. Open variant uses the rotating-light siren so the channel
    notices."""
    task_id = "abcdef12-3456-7890-abcd-ef1234567890"
    record = _open_record(task_id=task_id, reason="stuck_task_sweep")
    body = _format_slack_body(record)
    assert ":rotating_light:" in body["text"]
    assert "abcdef12" in body["text"]
    # Full UUID would be wasted bytes on a channel message.
    assert task_id not in body["text"]
    assert "stuck_task_sweep" in body["text"]
    assert "escalated to operator" in body["text"]


def test_slack_close_body_includes_mttr() -> None:
    """Close variant carries the white-check emoji and the MTTR value the
    sweep computed at emit time (so consumers don't recompute it)."""
    record = _close_record(
        task_id="11111111-2222-3333-4444-555555555555",
        close_reason="pr_merged",
        mttr_seconds=7200,
    )
    body = _format_slack_body(record)
    assert ":white_check_mark:" in body["text"]
    assert "11111111" in body["text"]
    assert "pr_merged" in body["text"]
    assert "mttr=7200s" in body["text"]


def test_slack_body_handles_missing_reason_gracefully() -> None:
    """Older emitters predate ADR-0058's reason taxonomy — the formatter
    must not crash on a missing field; ``unknown`` is the documented
    fallback."""
    record = _open_record()
    record["payload"] = {}
    body = _format_slack_body(record)
    assert "reason=unknown" in body["text"]


# ── Gating ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_ignores_non_task_events() -> None:
    """Non-task events (e.g. ``step.completed``) are common on the
    in-process broadcaster — the subscriber must drop them without
    POSTing anywhere."""
    client = _mock_http_client()
    fanout = _make_fanout(slack="https://hooks.slack/x", raw=[], http_client=client)
    await fanout.handle({
        "entity_type": "step",
        "action": "completed",
        "task_id": str(uuid.uuid4()),
        "payload": {},
    })
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_ignores_other_task_actions() -> None:
    """``task.registered`` / ``task.cancelled`` etc. flow through the
    broadcaster too — only the two escalation verbs trigger fan-out."""
    client = _mock_http_client()
    fanout = _make_fanout(
        slack="https://hooks.slack/x",
        raw=["https://webhook.example/x"],
        http_client=client,
    )
    await fanout.handle({
        "entity_type": "task",
        "action": "registered",
        "task_id": str(uuid.uuid4()),
        "payload": {},
    })
    client.post.assert_not_awaited()


# ── Slack POST shape ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slack_post_on_open_event() -> None:
    """``task.escalated_to_operator`` lands as one POST to the Slack URL
    with the Slack-shaped JSON body."""
    client = _mock_http_client()
    fanout = _make_fanout(
        slack="https://hooks.slack.example/AAA/BBB",
        http_client=client,
    )
    record = _open_record(reason="gate-broken")
    await fanout.handle(record)
    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args == ("https://hooks.slack.example/AAA/BBB",)
    body = kwargs["json"]
    assert "text" in body
    assert "escalated to operator" in body["text"]
    assert "gate-broken" in body["text"]
    assert ":rotating_light:" in body["text"]


@pytest.mark.asyncio
async def test_slack_post_on_close_event() -> None:
    """``task.escalation_closed`` lands as one POST with MTTR in the text."""
    client = _mock_http_client()
    fanout = _make_fanout(
        slack="https://hooks.slack.example/AAA/BBB",
        http_client=client,
    )
    record = _close_record(close_reason="re_progressed", mttr_seconds=600)
    await fanout.handle(record)
    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args == ("https://hooks.slack.example/AAA/BBB",)
    assert "escalation closed" in kwargs["json"]["text"]
    assert "mttr=600s" in kwargs["json"]["text"]
    assert "re_progressed" in kwargs["json"]["text"]


# ── Raw-webhook fan-out ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_raw_webhook_receives_raw_event_json() -> None:
    """Each URL in ``TREADMILL_NOTIFICATION_WEBHOOKS`` gets the raw event
    record posted as JSON (no Slack wrapping). All URLs are hit on a
    single event."""
    client = _mock_http_client()
    urls = [
        "https://webhook.example/one",
        "https://webhook.example/two",
        "https://webhook.example/three",
    ]
    fanout = _make_fanout(raw=urls, http_client=client)
    record = _open_record(reason="architect_cap")
    await fanout.handle(record)
    assert client.post.await_count == 3
    posted_urls = [call.args[0] for call in client.post.call_args_list]
    assert posted_urls == urls
    for call in client.post.call_args_list:
        # Body is the raw event record, not the Slack-wrapped shape.
        assert call.kwargs["json"] == record


@pytest.mark.asyncio
async def test_raw_webhooks_receive_close_event_json() -> None:
    """Close events fan out to raw webhooks identically to open events —
    the generic-webhook surface is symmetric across the two verbs."""
    client = _mock_http_client()
    fanout = _make_fanout(
        raw=["https://webhook.example/x"],
        http_client=client,
    )
    record = _close_record(close_reason="cancelled", mttr_seconds=42)
    await fanout.handle(record)
    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args == ("https://webhook.example/x",)
    assert kwargs["json"] == record


# ── Failure isolation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_failing_raw_webhook_does_not_block_others() -> None:
    """ADR-0062 Step 4 invariant: a single bad URL never blocks the
    others. We make the middle URL raise; the first + third URLs still
    get their POSTs."""
    client = _mock_http_client()
    urls = ["https://good/one", "https://bad/two", "https://good/three"]

    async def _post_side_effect(url: str, **_: Any) -> Any:
        if url == "https://bad/two":
            raise RuntimeError("simulated transport failure")
        return MagicMock()

    client.post.side_effect = _post_side_effect

    fanout = _make_fanout(raw=urls, http_client=client)
    record = _open_record()
    # Must not propagate the simulated failure.
    await fanout.handle(record)
    assert client.post.await_count == 3
    assert [call.args[0] for call in client.post.call_args_list] == urls


@pytest.mark.asyncio
async def test_slack_failure_does_not_block_raw_webhooks() -> None:
    """A failing Slack POST does not poison the generic-webhook fan-out
    that runs after it."""
    client = _mock_http_client()
    slack_url = "https://hooks.slack/fail"
    raw_url = "https://webhook.example/x"

    async def _post_side_effect(url: str, **_: Any) -> Any:
        if url == slack_url:
            raise RuntimeError("slack down")
        return MagicMock()

    client.post.side_effect = _post_side_effect

    fanout = _make_fanout(slack=slack_url, raw=[raw_url], http_client=client)
    record = _open_record()
    await fanout.handle(record)
    assert client.post.await_count == 2
    # The raw webhook still got the record.
    raw_call = client.post.call_args_list[1]
    assert raw_call.args == (raw_url,)
    assert raw_call.kwargs["json"] == record


@pytest.mark.asyncio
async def test_raw_webhook_failure_does_not_block_slack() -> None:
    """Symmetric guard: if the brief inverted the per-target loop order
    in a future refactor, a failing raw URL must still leave Slack alone.

    Today Slack runs first, so this is a forward-compat invariant — the
    test asserts both POSTs land regardless of which side errored.
    """
    client = _mock_http_client()
    slack_url = "https://hooks.slack/ok"
    raw_url = "https://webhook.example/fail"

    async def _post_side_effect(url: str, **_: Any) -> Any:
        if url == raw_url:
            raise RuntimeError("raw down")
        return MagicMock()

    client.post.side_effect = _post_side_effect

    fanout = _make_fanout(slack=slack_url, raw=[raw_url], http_client=client)
    record = _open_record()
    await fanout.handle(record)
    assert client.post.await_count == 2
    posted_urls = {call.args[0] for call in client.post.call_args_list}
    assert posted_urls == {slack_url, raw_url}


# ── Settings wiring ───────────────────────────────────────────────────────────


def test_make_notification_fanout_reads_settings_fields() -> None:
    """The factory pulls ``slack_webhook_url`` and the parsed
    ``notification_webhook_urls`` list off ``Settings``."""
    settings = MagicMock()
    settings.slack_webhook_url = "https://hooks.slack/X"
    settings.notification_webhook_urls = [
        "https://webhook.example/a",
        "https://webhook.example/b",
    ]
    fanout = make_notification_fanout(settings)
    assert fanout.slack_webhook_url == "https://hooks.slack/X"
    assert fanout.raw_webhook_urls == [
        "https://webhook.example/a",
        "https://webhook.example/b",
    ]
    assert fanout.is_configured is True


def test_make_notification_fanout_handles_empty_config() -> None:
    """No env vars set → no targets, ``is_configured`` False, so the
    lifespan handler's ``start()`` short-circuit fires."""
    settings = MagicMock()
    settings.slack_webhook_url = None
    settings.notification_webhook_urls = []
    fanout = make_notification_fanout(settings)
    assert fanout.slack_webhook_url is None
    assert fanout.raw_webhook_urls == []
    assert fanout.is_configured is False


def test_settings_parses_comma_separated_webhooks(monkeypatch) -> None:
    """``TREADMILL_NOTIFICATION_WEBHOOKS=a,b,c`` parses to a 3-URL list;
    extra whitespace and empty fragments are tolerated."""
    from treadmill_api.config import Settings

    monkeypatch.setenv(
        "TREADMILL_NOTIFICATION_WEBHOOKS",
        " https://one,https://two , https://three ,",
    )
    settings = Settings()
    assert settings.notification_webhook_urls == [
        "https://one",
        "https://two",
        "https://three",
    ]


def test_settings_defaults_to_no_targets(monkeypatch) -> None:
    """Both fields default to unset / empty so a deployment without the
    env vars opts out cleanly."""
    from treadmill_api.config import Settings

    monkeypatch.delenv("TREADMILL_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TREADMILL_NOTIFICATION_WEBHOOKS", raising=False)
    settings = Settings()
    assert settings.slack_webhook_url is None
    assert settings.notification_webhook_urls == []


# ── Lifecycle ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_is_noop_when_unconfigured() -> None:
    """No targets means no subscription, no task, no http client — the
    lifespan handler treats this as a clean disable."""
    fanout = NotificationFanout(slack_webhook_url=None, raw_webhook_urls=[])
    await fanout.start()
    assert fanout._task is None
    assert fanout._queue is None
    # ``stop`` on a never-started instance is also a no-op (no raise).
    await fanout.stop()


@pytest.mark.asyncio
async def test_start_subscribes_and_stop_unsubscribes() -> None:
    """Configured fanout registers a queue with the eventbus broadcaster
    on ``start`` and removes it on ``stop`` — verified by counting the
    module-level subscriber dict."""
    from treadmill_api import eventbus

    client = _mock_http_client()
    fanout = NotificationFanout(
        slack_webhook_url="https://hooks.slack/X",
        raw_webhook_urls=[],
        http_client=client,
    )
    before = len(eventbus._local_subscribers)
    await fanout.start()
    assert len(eventbus._local_subscribers) == before + 1
    await fanout.stop()
    assert len(eventbus._local_subscribers) == before


@pytest.mark.asyncio
async def test_owned_http_client_is_closed_on_stop() -> None:
    """When no client is injected, the fanout owns the
    ``httpx.AsyncClient`` and must close it on shutdown so connections
    aren't leaked across a server restart."""
    fanout = NotificationFanout(
        slack_webhook_url="https://hooks.slack/X",
        raw_webhook_urls=[],
    )
    await fanout.start()
    owned = fanout._http_client
    assert owned is not None
    await fanout.stop()
    # The client is closed AND the reference is cleared so a subsequent
    # ``handle`` cannot accidentally reuse a torn-down client.
    assert fanout._http_client is None


@pytest.mark.asyncio
async def test_injected_http_client_is_not_closed_on_stop() -> None:
    """When tests (or a future deployment) inject a shared client, the
    fanout must leave its lifecycle alone."""
    client = _mock_http_client()
    fanout = NotificationFanout(
        slack_webhook_url="https://hooks.slack/X",
        raw_webhook_urls=[],
        http_client=client,
    )
    await fanout.start()
    await fanout.stop()
    client.aclose.assert_not_awaited()


@pytest.mark.asyncio
async def test_published_event_reaches_fanout_via_broadcaster() -> None:
    """End-to-end through the in-process broadcaster: pushing a record
    into ``_broadcast_local`` (the same seam ``LoggingEventPublisher``
    and ``SNSEventPublisher`` fan through) wakes the subscriber and
    drives the documented POST."""
    import asyncio

    from treadmill_api.eventbus import _broadcast_local

    client = _mock_http_client()
    fanout = NotificationFanout(
        slack_webhook_url="https://hooks.slack/X",
        raw_webhook_urls=["https://webhook.example/x"],
        http_client=client,
    )
    await fanout.start()
    try:
        record = _open_record(reason="architect_cap")
        _broadcast_local(record)
        # Give the loop a chance to drain the queue + run the handler.
        for _ in range(20):
            await asyncio.sleep(0.01)
            if client.post.await_count >= 2:
                break
        assert client.post.await_count == 2
        posted_urls = {call.args[0] for call in client.post.call_args_list}
        assert posted_urls == {
            "https://hooks.slack/X",
            "https://webhook.example/x",
        }
    finally:
        await fanout.stop()
