"""Handler-level unit tests for the webhook-inbox poller (Phase C.1, ADR-0017).

Mirrors ``test_consumer_unit.py``'s shape: stub SQS + stub sessionmaker
+ stub publisher, drive the poller's per-message processing path
deterministically, assert on side effects. The integration test in
``test_integration_webhook_inbox.py`` covers the live-moto + real-Postgres
chain.

Behaviors exercised here:

* Happy path — well-formed envelope + valid signature → Event row inserted
  with the deterministic event_id, publisher invoked, SQS message deleted.
* Idempotency — the same ``x-github-delivery`` arriving twice derives the
  same event_id; the second arrival's INSERT hits ON CONFLICT DO NOTHING
  and the publisher still fires (the bus is notification-only).
* Malformed envelope — deleted poison-safe; no Event row, no publish; the
  log message MUST NOT contain the raw body (the body could carry secrets
  or PII from a misrouted source).
* Missing ``x-github-delivery`` — deleted poison-safe; no Event row.
* Signature failure — deleted poison-safe; the log message MUST NOT
  contain the body, repo, PR number, or any envelope header value.
* Empty SQS receive — no crash, loop continues.
* Exponential backoff — consecutive ``receive_message`` failures sleep
  for the ``1, 2, 4, 8, 16, 30`` ladder; the cap holds.
* Probe staleness — the ``is_stale`` helper flips to True once the
  watermark is older than the threshold.

Stubbing strategy mirrors ``test_consumer_unit.py``: a tiny session that
records execute / commit, a tiny sessionmaker factory, a tiny SQS pattern
class.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.webhook_inbox import (
    WebhookInboxPoller,
    _header_lookup,
)
from treadmill_api.dependencies import (
    ProbeStatus,
    WebhookInboxProbe,
)


# ── Stubs ─────────────────────────────────────────────────────────────────────


class _StubSession:
    """Records execute / commit / get calls so tests can assert on DB work."""

    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.commit = AsyncMock()
        self.get = AsyncMock()


def _stub_factory(session: _StubSession) -> Any:
    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    def _make() -> Any:
        return _cm()

    return _make


class _StubSqs:
    """Records receive_message / delete_message calls; replays canned receive
    responses."""

    def __init__(self, receive_responses: list[Any] | None = None) -> None:
        self._responses = list(receive_responses or [])
        self.receive_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.receive_calls.append(kwargs)
        if not self._responses:
            return {"Messages": []}
        next_ = self._responses.pop(0)
        if isinstance(next_, Exception):
            raise next_
        return next_

    def delete_message(self, **kwargs: Any) -> None:
        self.delete_calls.append(kwargs)


class _StubSecretsManager:
    """Returns a canned secret value; records the SecretId lookup."""

    def __init__(self, secret: str = "test-webhook-secret") -> None:
        self.secret = secret
        self.calls: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict[str, Any]:
        self.calls.append(SecretId)
        return {"SecretString": self.secret}


class _StubPublisher:
    """Records publish calls so tests can assert fan-out happened."""

    def __init__(self) -> None:
        self.published: list[tuple[Any, Any]] = []

    async def publish(self, event: Any, payload: Any) -> None:
        self.published.append((event, payload))


# ── Helpers ───────────────────────────────────────────────────────────────────


PR_OPENED_PAYLOAD: dict[str, Any] = {
    "action": "opened",
    "pull_request": {
        "number": 42,
        "title": "feat: add the thing",
        "head": {"ref": "feat/thing", "sha": "deadbeef" * 5},
        "merged": False,
    },
    "repository": {"full_name": "joe/treadmill"},
    "sender": {"login": "joe"},
}


def _envelope(
    *,
    github_event: str = "pull_request",
    delivery: str | None = "12345678-1234-5678-1234-567812345678",
    signature: str = "sha256=ignored-in-tests",
    body: dict[str, Any] | None = None,
) -> str:
    """Build a JSON envelope of the shape the Lambda emits."""
    headers: dict[str, str] = {}
    if github_event:
        headers["x-github-event"] = github_event
    if delivery is not None:
        headers["x-github-delivery"] = delivery
    if signature:
        headers["x-hub-signature-256"] = signature
    return json.dumps({
        "headers": headers,
        "body": json.dumps(body if body is not None else PR_OPENED_PAYLOAD),
    })


def _sqs_message(envelope_body: str, *, message_id: str = "msg-1") -> dict[str, Any]:
    return {
        "MessageId": message_id,
        "ReceiptHandle": f"rh-{message_id}",
        "Body": envelope_body,
    }


def _make_poller(
    *,
    session: _StubSession | None = None,
    publisher: _StubPublisher | None = None,
    sqs: _StubSqs | None = None,
    secrets_manager: _StubSecretsManager | None = None,
    verifier: Any = None,
    normalizer: Any = None,
    staleness_seconds: float = 120.0,
    redis_client: Any = None,
) -> WebhookInboxPoller:
    session = session if session is not None else _StubSession()
    publisher = publisher if publisher is not None else _StubPublisher()
    sqs = sqs if sqs is not None else _StubSqs()
    secrets_manager = (
        secrets_manager if secrets_manager is not None else _StubSecretsManager()
    )

    # ``session.get`` returns a fake Event with the requested id (so the
    # poller's publish path has something to hand the publisher).
    async def _get(model: Any, key: Any) -> Any:
        m = MagicMock()
        m.id = key
        m.task_id = None
        m.plan_id = None
        m.run_id = None
        m.step_id = None
        m.entity_type = "github"
        m.action = "pr_opened"
        return m

    session.get = AsyncMock(side_effect=_get)

    poller = WebhookInboxPoller(
        sqs_client=sqs,
        queue_url="https://sqs.example.com/webhook-inbox",
        secrets_manager_client=secrets_manager,
        webhook_secret_name="treadmill-test/github-webhook-secret",
        sessionmaker=_stub_factory(session),  # type: ignore[arg-type]
        publisher=publisher,
        verifier=verifier if verifier is not None else (lambda *_a, **_k: None),
        normalizer=normalizer,
        staleness_seconds=staleness_seconds,
        redis_client=redis_client,
    )
    # Set the cached secret directly (would normally be fetched on start()).
    poller._webhook_secret = secrets_manager.secret
    return poller


class _StubRedis:
    """Minimal async Redis stub that records rpush + expire calls.

    Mirrors the surface ``buffer_pending_event`` touches: ``rpush`` +
    ``expire``. Both are recorded so tests can assert the buffer was
    invoked with the expected key.
    """

    def __init__(self) -> None:
        self.rpush_calls: list[tuple[str, Any]] = []
        self.expire_calls: list[tuple[str, int]] = []

    async def rpush(self, key: str, value: Any) -> int:
        self.rpush_calls.append((key, value))
        return len(self.rpush_calls)

    async def expire(self, key: str, seconds: int) -> bool:
        self.expire_calls.append((key, seconds))
        return True


# ── Header lookup ─────────────────────────────────────────────────────────────


def test_header_lookup_is_case_insensitive() -> None:
    headers = {"X-GitHub-Event": "pull_request"}
    assert _header_lookup(headers, "x-github-event") == "pull_request"
    assert _header_lookup(headers, "X-GITHUB-EVENT") == "pull_request"
    assert _header_lookup(headers, "missing") is None


# ── Happy path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_happy_path_persists_publishes_and_deletes() -> None:
    """Well-formed envelope + signature accepted → DB INSERT + publish + delete."""
    session = _StubSession()
    publisher = _StubPublisher()
    sqs = _StubSqs()
    poller = _make_poller(session=session, publisher=publisher, sqs=sqs)

    msg = _sqs_message(_envelope())
    await poller._process(msg)

    # Two execute calls: the task_prs SELECT lookup (added 2026-05-14 to
    # match the HTTP route's task_id stamping) + the Event INSERT. Pre-fix
    # this was 1 call; the dual-ingress drift surfaced when downstream
    # tasks stayed deferred because ``events.task_id`` was NULL on every
    # github event. See task #117 for the facade unification.
    assert session.execute.await_count == 2
    session.commit.assert_awaited()
    assert len(publisher.published) == 1
    assert sqs.delete_calls == [
        {"QueueUrl": poller.queue_url, "ReceiptHandle": "rh-msg-1"}
    ]


@pytest.mark.asyncio
async def test_process_derives_deterministic_event_id_from_delivery() -> None:
    """The event_id ID is ``uuid5(NAMESPACE_OID, x-github-delivery)`` — same
    delivery UUID always derives the same Event PK. The session.get call
    sees that PK; we assert it matches the expected uuid5."""
    session = _StubSession()
    poller = _make_poller(session=session)

    delivery = "11111111-2222-3333-4444-555555555555"
    expected_id = uuid.uuid5(uuid.NAMESPACE_OID, delivery)

    msg = _sqs_message(_envelope(delivery=delivery))
    await poller._process(msg)

    # ``session.get(Event, event_id)`` was called with the derived UUID.
    session.get.assert_awaited()
    call_args = session.get.await_args
    assert call_args.args[1] == expected_id


@pytest.mark.asyncio
async def test_process_idempotent_on_redelivery() -> None:
    """The same x-github-delivery arriving twice derives the same event_id;
    both arrivals follow the same code path (ON CONFLICT DO NOTHING at the
    DB layer collapses the duplicate). We verify the two derived event_ids
    match via the session.get inspect."""
    session = _StubSession()
    poller = _make_poller(session=session)

    delivery = "deadbeef-dead-beef-dead-beefdeadbeef"
    msg1 = _sqs_message(_envelope(delivery=delivery), message_id="msg-A")
    msg2 = _sqs_message(_envelope(delivery=delivery), message_id="msg-B")
    await poller._process(msg1)
    await poller._process(msg2)

    # Two persist attempts → two session.get calls, both with the same UUID.
    assert session.get.await_count == 2
    derived_uuids = [c.args[1] for c in session.get.await_args_list]
    assert derived_uuids[0] == derived_uuids[1]
    # And both delete the SQS message.
    assert {c["ReceiptHandle"] for c in poller.sqs.delete_calls} == {
        "rh-msg-A", "rh-msg-B",
    }


# ── Malformed envelope ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_malformed_envelope_is_deleted_and_logs_no_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A JSON-malformed envelope is deleted poison-safe. No Event row is
    written; no publish fires. The log message MUST NOT include the raw
    body (could leak secrets from a misrouted source)."""
    session = _StubSession()
    publisher = _StubPublisher()
    sqs = _StubSqs()
    poller = _make_poller(session=session, publisher=publisher, sqs=sqs)

    poison_body = "this is not json {{{ secret_token=hunter2 }}}"
    msg = _sqs_message(poison_body, message_id="msg-bad")
    with caplog.at_level(logging.WARNING, logger="treadmill.webhook_inbox"):
        await poller._process(msg)

    # No DB work; no publish; message deleted.
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
    assert publisher.published == []
    assert sqs.delete_calls == [
        {"QueueUrl": poller.queue_url, "ReceiptHandle": "rh-msg-bad"}
    ]
    # Body MUST NOT appear in any log record's formatted message.
    full_log = " ".join(rec.getMessage() for rec in caplog.records)
    assert "hunter2" not in full_log
    assert "secret_token" not in full_log


@pytest.mark.asyncio
async def test_process_envelope_with_extra_fields_is_rejected_poison_safe() -> None:
    """The Pydantic envelope is ``extra='forbid'`` — an unknown top-level
    key fails validation and the message is poison-safe deleted."""
    session = _StubSession()
    sqs = _StubSqs()
    poller = _make_poller(session=session, sqs=sqs)

    body = json.dumps({"headers": {}, "body": "", "extra": "no"})
    await poller._process(_sqs_message(body, message_id="extra-key"))

    session.execute.assert_not_awaited()
    assert len(sqs.delete_calls) == 1


# ── Missing x-github-delivery ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_missing_delivery_header_is_deleted() -> None:
    """An envelope without ``x-github-delivery`` cannot derive an event_id;
    the audit-log idempotency contract is unenforceable. Drop poison-safe."""
    session = _StubSession()
    sqs = _StubSqs()
    poller = _make_poller(session=session, sqs=sqs)

    await poller._process(_sqs_message(_envelope(delivery=None), message_id="no-delv"))

    session.execute.assert_not_awaited()
    assert len(sqs.delete_calls) == 1


@pytest.mark.asyncio
async def test_process_empty_delivery_header_is_deleted() -> None:
    """An empty-string x-github-delivery is treated the same as missing."""
    session = _StubSession()
    poller = _make_poller(session=session)

    await poller._process(_sqs_message(_envelope(delivery=""), message_id="empty-delv"))

    session.execute.assert_not_awaited()


# ── Signature failure ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_signature_failure_does_not_leak_body_or_repo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Signature verification failure logs ONLY the SQS message ID and the
    derived event_id. The body, repo, PR number, and signature header MUST
    NOT appear in any log record (a misrouted employer webhook would
    otherwise leak metadata into the wrong deployment's CloudWatch).

    We use a verifier stub that always raises InvalidSignatureError so the
    test doesn't depend on the live HMAC implementation.
    """
    from treadmill_api.webhooks.signatures import InvalidSignatureError

    session = _StubSession()
    publisher = _StubPublisher()
    sqs = _StubSqs()

    def _always_fail(*_a: Any, **_k: Any) -> None:
        raise InvalidSignatureError("does not match")

    poller = _make_poller(
        session=session, publisher=publisher, sqs=sqs, verifier=_always_fail,
    )

    secret_body = {
        "action": "opened",
        "pull_request": {"number": 999, "title": "TOP SECRET"},
        "repository": {"full_name": "employer/private-repo"},
        "sender": {"login": "ceo"},
    }
    envelope = _envelope(
        body=secret_body,
        signature="sha256=very-private-signature-value",
    )
    msg = _sqs_message(envelope, message_id="sig-fail-msg")

    with caplog.at_level(logging.WARNING, logger="treadmill.webhook_inbox"):
        await poller._process(msg)

    # No DB work; no publish; message deleted.
    session.execute.assert_not_awaited()
    assert publisher.published == []
    assert len(sqs.delete_calls) == 1

    # Scrub-assertions — the leaked-metadata strings MUST NOT appear in any
    # log record's formatted message or its args.
    forbidden = [
        "employer/private-repo",
        "TOP SECRET",
        "ceo",
        "999",
        "very-private-signature-value",
        "private",
    ]
    full_log = " ".join(rec.getMessage() for rec in caplog.records)
    for needle in forbidden:
        assert needle not in full_log, (
            f"signature-failure log leaked {needle!r}: {full_log!r}"
        )


# ── Unhandled github event ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_unhandled_github_event_is_deleted_no_db() -> None:
    """A GitHub event the normalizer doesn't recognize (e.g., ``ping``) is
    acknowledged (deleted) but produces no Event row or publish — same
    contract as the HTTP route's ``status=skipped`` response."""
    session = _StubSession()
    publisher = _StubPublisher()
    sqs = _StubSqs()
    poller = _make_poller(session=session, publisher=publisher, sqs=sqs)

    await poller._process(_sqs_message(_envelope(github_event="ping")))

    session.execute.assert_not_awaited()
    assert publisher.published == []
    assert len(sqs.delete_calls) == 1


# ── Missing x-github-event header ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_missing_github_event_header_is_deleted() -> None:
    """Without ``x-github-event``, normalization can't run; drop poison-safe."""
    session = _StubSession()
    sqs = _StubSqs()
    poller = _make_poller(session=session, sqs=sqs)

    await poller._process(_sqs_message(_envelope(github_event="")))

    session.execute.assert_not_awaited()
    assert len(sqs.delete_calls) == 1


# ── Body-not-JSON ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_envelope_body_not_json_is_deleted() -> None:
    """The envelope is well-formed but its ``body`` field isn't valid JSON
    — GitHub always sends JSON; this is a misrouted source. Drop poison-safe."""
    session = _StubSession()
    sqs = _StubSqs()
    poller = _make_poller(session=session, sqs=sqs)

    envelope_json = json.dumps({
        "headers": {
            "x-github-event": "pull_request",
            "x-github-delivery": "0000-bad-body-json",
            "x-hub-signature-256": "sha256=x",
        },
        "body": "<<<not json>>>",
    })
    await poller._process(_sqs_message(envelope_json))

    session.execute.assert_not_awaited()
    assert len(sqs.delete_calls) == 1


# ── Exponential backoff ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_backs_off_exponentially_on_consecutive_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive ``receive_message`` failures sleep for 1, 2, 4, 8, 16,
    30, 30, ... seconds. Mirrors ``CoordinationConsumer``'s ladder."""
    sleeps: list[float] = []

    poller = _make_poller()

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 8:
            poller._stopped = True

    monkeypatch.setattr(
        "treadmill_api.coordination.webhook_inbox.asyncio.sleep",
        _fake_sleep,
    )

    class _AlwaysFail:
        def receive_message(self, **_k: Any) -> dict[str, Any]:
            raise RuntimeError("always fail")

    poller.sqs = _AlwaysFail()
    poller._stopped = False

    await poller._run()

    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30]


@pytest.mark.asyncio
async def test_run_resets_backoff_after_successful_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful poll between failures resets the counter so the next
    failure starts the ladder at 1s again."""
    sleeps: list[float] = []
    poller = _make_poller()

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 4:
            poller._stopped = True

    monkeypatch.setattr(
        "treadmill_api.coordination.webhook_inbox.asyncio.sleep",
        _fake_sleep,
    )

    class _Seq:
        def __init__(self, pattern: list[bool]) -> None:
            self.pattern = list(pattern)

        def receive_message(self, **_k: Any) -> dict[str, Any]:
            if not self.pattern:
                return {"Messages": []}
            if self.pattern.pop(0):
                raise RuntimeError("fail")
            return {"Messages": []}

    # 2 fails → success → 2 fails. Expect sleeps 1, 2, (no sleep), 1, 2.
    poller.sqs = _Seq([True, True, False, True, True])
    poller._stopped = False

    await poller._run()

    assert sleeps[:2] == [1, 2]
    assert sleeps[2] == 1
    assert sleeps[3] == 2


# ── Empty receive does not crash ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_handles_empty_receive_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SQS receive that returns no Messages is a steady-state outcome —
    long-poll timeouts. Loop continues, no health degradation."""
    poller = _make_poller()
    poll_count = 0

    class _AlwaysEmpty:
        def receive_message(self, **_k: Any) -> dict[str, Any]:
            nonlocal poll_count
            poll_count += 1
            if poll_count >= 3:
                poller._stopped = True
            return {"Messages": []}

    async def _fake_sleep(_d: float) -> None:
        pass

    monkeypatch.setattr(
        "treadmill_api.coordination.webhook_inbox.asyncio.sleep",
        _fake_sleep,
    )

    poller.sqs = _AlwaysEmpty()
    await poller._run()

    assert poll_count == 3
    assert poller.status_for_health() == "running"


# ── Health status ────────────────────────────────────────────────────────────


def test_status_for_health_starts_as_starting() -> None:
    poller = _make_poller()
    assert poller.status_for_health() == "starting"


@pytest.mark.asyncio
async def test_status_for_health_dead_when_task_died_unexpectedly() -> None:
    """If ``_run`` exited (task is done) without ``stop()`` flipping
    ``_stopped``, ``status_for_health`` reports ``dead``."""
    import asyncio as _asyncio

    poller = _make_poller()

    async def _explode() -> None:
        raise RuntimeError("explode")

    poller._task = _asyncio.create_task(_explode())
    try:
        await poller._task
    except RuntimeError:
        pass

    assert poller.status_for_health() == "dead"
    assert poller.is_running() is False


# ── Staleness probe ──────────────────────────────────────────────────────────


def test_is_stale_returns_false_when_never_polled() -> None:
    """Before any poll has succeeded, staleness can't be measured; report
    False so the probe doesn't flip-flop at startup."""
    poller = _make_poller()
    assert poller.is_stale() is False


def test_is_stale_returns_true_after_threshold_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a successful poll set the watermark, ``is_stale`` flips True
    once ``staleness_seconds`` elapses."""
    poller = _make_poller(staleness_seconds=10.0)
    # Simulate a poll succeeding 100 seconds ago.
    now = 1000.0
    poller._last_success_monotonic = now - 100.0
    monkeypatch.setattr(
        "treadmill_api.coordination.webhook_inbox.time.monotonic",
        lambda: now,
    )
    assert poller.is_stale() is True


def test_is_stale_returns_false_just_under_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poller = _make_poller(staleness_seconds=120.0)
    now = 1000.0
    poller._last_success_monotonic = now - 30.0
    monkeypatch.setattr(
        "treadmill_api.coordination.webhook_inbox.time.monotonic",
        lambda: now,
    )
    assert poller.is_stale() is False


# ── WebhookInboxProbe wiring ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_reports_not_configured_when_poller_none() -> None:
    probe = WebhookInboxProbe(None)
    result = await probe.check()
    assert result.status is ProbeStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_probe_reports_unreachable_when_task_not_running() -> None:
    poller = _make_poller()
    # Never started — ``is_running`` returns False.
    probe = WebhookInboxProbe(poller)
    result = await probe.check()
    assert result.status is ProbeStatus.UNREACHABLE
    assert result.detail is not None
    assert "not running" in result.detail


@pytest.mark.asyncio
async def test_probe_reports_unreachable_when_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poller whose task is alive but whose last-success watermark is
    older than the staleness threshold reports unreachable — captures the
    "wedged long-poll thread" failure mode."""
    import asyncio as _asyncio

    poller = _make_poller(staleness_seconds=5.0)

    async def _sleep_forever() -> None:
        await _asyncio.sleep(3600)

    poller._task = _asyncio.create_task(_sleep_forever())
    try:
        # Set watermark 100s in the past.
        now = 1000.0
        poller._last_success_monotonic = now - 100.0
        monkeypatch.setattr(
            "treadmill_api.coordination.webhook_inbox.time.monotonic",
            lambda: now,
        )

        probe = WebhookInboxProbe(poller)
        result = await probe.check()
        assert result.status is ProbeStatus.UNREACHABLE
        assert result.detail is not None
        assert "successful SQS poll" in result.detail
    finally:
        poller._task.cancel()
        try:
            await poller._task
        except _asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_probe_reports_ok_when_running_and_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio as _asyncio

    poller = _make_poller(staleness_seconds=120.0)

    async def _sleep_forever() -> None:
        await _asyncio.sleep(3600)

    poller._task = _asyncio.create_task(_sleep_forever())
    try:
        now = 1000.0
        poller._last_success_monotonic = now - 5.0
        monkeypatch.setattr(
            "treadmill_api.coordination.webhook_inbox.time.monotonic",
            lambda: now,
        )

        probe = WebhookInboxProbe(poller)
        result = await probe.check()
        assert result.status is ProbeStatus.OK
    finally:
        poller._task.cancel()
        try:
            await poller._task
        except _asyncio.CancelledError:
            pass


# ── ADR-0063 Step 1: cache-then-heal buffer-on-miss ──────────────────────────


def _session_with_task_prs_miss() -> _StubSession:
    """Build a stub session whose task_prs SELECT returns no row.

    The poller's ``_persist_and_publish`` calls ``execute`` twice — first
    the ``task_prs`` lookup, then the Event INSERT. The lookup uses
    ``result.scalar_one_or_none()`` to read the resolved ``task_id``; we
    rig that to return None so the buffer-on-miss branch fires.
    """
    session = _StubSession()
    lookup_result = MagicMock()
    lookup_result.scalar_one_or_none = MagicMock(return_value=None)
    insert_result = MagicMock()
    session.execute = AsyncMock(side_effect=[lookup_result, insert_result])
    return session


@pytest.mark.asyncio
async def test_process_task_prs_miss_buffers_pending_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the task_prs SELECT misses on a pr_opened, the SQS-ingress
    mirrors the HTTP route's cache-then-heal: the Event row persists
    with ``task_id=NULL`` AND the event_id is buffered on the (repo,
    pr_number) pending list. The buffer-log lands at INFO so operators
    can confirm the new path took effect.

    This is the ADR-0063 Step 1 invariant — the dual-ingress drift the
    SQS path was carrying since ADR-0049.
    """
    session = _session_with_task_prs_miss()
    redis = _StubRedis()
    poller = _make_poller(session=session, redis_client=redis)

    delivery = "12345678-1234-5678-1234-567812345678"
    expected_event_id = uuid.uuid5(uuid.NAMESPACE_OID, delivery)
    msg = _sqs_message(_envelope(delivery=delivery))

    with caplog.at_level(logging.INFO, logger="treadmill.webhook_inbox"):
        await poller._process(msg)

    # Exactly one rpush against the (repo, pr_number) buffer key. The
    # PR_OPENED_PAYLOAD has repo="joe/treadmill" + pr=42.
    assert len(redis.rpush_calls) == 1
    key, raw = redis.rpush_calls[0]
    assert key == "pr:joe/treadmill:42:pending_events"
    # The buffered record is JSON carrying the derived event_id.
    record = json.loads(raw)
    assert record["event_id"] == str(expected_event_id)
    # TTL armed on the same key.
    assert redis.expire_calls and redis.expire_calls[0][0] == key

    # The buffer-took-effect log line lands at INFO so the operator can
    # confirm the mirror is active. Filter to the webhook_inbox logger
    # specifically — the inner ``buffer_pending_event`` helper logs from
    # ``treadmill.pending_events`` with a similar message and would
    # otherwise satisfy a broader substring match without proving the
    # poller's own confirmation log fired.
    inbox_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.INFO
        and rec.name == "treadmill.webhook_inbox"
    ]
    assert any(
        "webhook inbox: buffered pending event_id=" in line
        for line in inbox_lines
    )


@pytest.mark.asyncio
async def test_process_task_prs_hit_does_not_buffer() -> None:
    """When the task_prs SELECT resolves, the buffer is NOT touched —
    the Event row stamps ``task_id`` from the lookup and the cache-
    then-heal path is dormant."""
    session = _StubSession()
    resolved_task_id = uuid.uuid4()
    lookup_result = MagicMock()
    lookup_result.scalar_one_or_none = MagicMock(return_value=resolved_task_id)
    insert_result = MagicMock()
    session.execute = AsyncMock(side_effect=[lookup_result, insert_result])

    redis = _StubRedis()
    poller = _make_poller(session=session, redis_client=redis)

    await poller._process(_sqs_message(_envelope()))

    assert redis.rpush_calls == []
    assert redis.expire_calls == []


@pytest.mark.asyncio
async def test_process_task_prs_miss_without_redis_client_does_not_crash() -> None:
    """Narrow tests + log-only deployments may wire the poller without a
    Redis client. A task_prs miss in that mode still persists the Event
    (with task_id=NULL) but skips the buffer call cleanly — no
    AttributeError on the missing client."""
    session = _session_with_task_prs_miss()
    poller = _make_poller(session=session, redis_client=None)

    # Should complete without raising; Event row still persisted.
    await poller._process(_sqs_message(_envelope()))

    # commit was reached → INSERT path ran end-to-end.
    session.commit.assert_awaited()


# ── Persist / publish error path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_does_not_delete_when_persist_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the DB INSERT raises (transient driver error, network blip), the
    SQS message is left for retry — mirrors the consumer's "transient
    errors bubble back to the queue" contract."""
    session = _StubSession()
    session.execute.side_effect = RuntimeError("DB blip")
    publisher = _StubPublisher()
    sqs = _StubSqs()
    poller = _make_poller(session=session, publisher=publisher, sqs=sqs)

    with caplog.at_level(logging.ERROR, logger="treadmill.webhook_inbox"):
        await poller._process(_sqs_message(_envelope()))

    # Persist raised → no publish → no delete.
    assert publisher.published == []
    assert sqs.delete_calls == []
    full_log = " ".join(rec.getMessage() for rec in caplog.records)
    assert "persist/publish failed" in full_log


# ── Secret-fetch on start() ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_fetches_and_caches_webhook_secret() -> None:
    """``start()`` calls Secrets Manager once and caches the value."""
    secrets = _StubSecretsManager(secret="from-secrets-manager")
    poller = _make_poller(secrets_manager=secrets)
    # Clear the value pre-set by ``_make_poller`` so we exercise the fetch.
    poller._webhook_secret = None

    await poller.start()
    try:
        assert poller._webhook_secret == "from-secrets-manager"
        assert secrets.calls == ["treadmill-test/github-webhook-secret"]
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_start_raises_when_secret_is_empty() -> None:
    """A Secrets Manager value of an empty string is a misconfiguration —
    fail-fast at startup rather than silently skipping all signature checks."""
    secrets = _StubSecretsManager(secret="")
    poller = _make_poller(secrets_manager=secrets)
    poller._webhook_secret = None
    with pytest.raises(RuntimeError, match="has no SecretString"):
        await poller.start()
