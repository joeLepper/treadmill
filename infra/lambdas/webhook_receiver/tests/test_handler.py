"""Unit tests for the webhook-receiver Lambda handler.

The handler module is import-time sensitive: ``QUEUE_URL`` is read from
``os.environ["WEBHOOK_INBOX_QUEUE_URL"]`` at module load. The env var is
seeded by ``conftest.py`` before pytest imports this file (see that
module's docstring for the rationale). Each test patches the module's
module-level ``sqs`` boto3 client so SQS calls are intercepted without
network I/O.

Coverage per Phase B.3:

1. Happy path with all three preserved headers and a JSON body.
2. Header filtering — non-preserved headers are dropped.
3. Case normalization — mixed-case header keys arrive lowercase.
4. Missing body — ``body=None`` becomes the empty string.
5. Missing headers — ``headers=None`` becomes the empty dict.
6. ``isBase64Encoded=True`` — body is base64-decoded to UTF-8.
7. ``isBase64Encoded=True`` with invalid base64 — body passes through
   unchanged (the poller's HMAC check will then fail → DLQ → operator).
8. Return value — ``{"statusCode": 202, "body": "queued"}``.
"""

from __future__ import annotations

import base64
import json
from unittest import mock

import pytest

# Imported after conftest.py has set ``WEBHOOK_INBOX_QUEUE_URL`` and put
# the Lambda's source directory on ``sys.path`` (mirroring the AWS
# Lambda runtime entry-point resolution: ``handler.handler``).
import handler as handler_mod  # noqa: E402


# Convenience: the module under test reads this constant once at import time.
QUEUE_URL = handler_mod.QUEUE_URL


def _make_event(
    *,
    headers: dict[str, str] | None,
    body: str | None,
    is_base64_encoded: bool = False,
) -> dict:
    """Build a synthetic API Gateway HTTP API v2 event payload.

    ``isBase64Encoded`` is only set when ``True`` to mirror real
    API Gateway behaviour (the key is absent for non-binary payloads).
    """
    event: dict = {"headers": headers, "body": body}
    if is_base64_encoded:
        event["isBase64Encoded"] = True
    return event


def _envelope_from_call(mock_sqs: mock.MagicMock) -> dict:
    """Extract the JSON envelope passed to ``sqs.send_message``."""
    mock_sqs.send_message.assert_called_once()
    kwargs = mock_sqs.send_message.call_args.kwargs
    assert kwargs["QueueUrl"] == QUEUE_URL
    return json.loads(kwargs["MessageBody"])


@pytest.fixture
def mock_sqs(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Replace the module's ``sqs`` client with a MagicMock for the test."""
    fake = mock.MagicMock(name="sqs_client")
    monkeypatch.setattr(handler_mod, "sqs", fake)
    return fake


# ── Happy path ────────────────────────────────────────────────────────────────


def test_happy_path_three_preserved_headers(mock_sqs: mock.MagicMock) -> None:
    """All three preserved headers plus a JSON body round-trip into the envelope."""
    body = json.dumps({"action": "opened", "number": 42})
    event = _make_event(
        headers={
            "x-github-event": "pull_request",
            "x-github-delivery": "00000000-0000-0000-0000-000000000001",
            "x-hub-signature-256": "sha256=deadbeef",
            "content-type": "application/json",
        },
        body=body,
    )

    result = handler_mod.handler(event, None)

    envelope = _envelope_from_call(mock_sqs)
    assert envelope["headers"] == {
        "x-github-event": "pull_request",
        "x-github-delivery": "00000000-0000-0000-0000-000000000001",
        "x-hub-signature-256": "sha256=deadbeef",
    }
    assert envelope["body"] == body
    assert result == {"statusCode": 202, "body": "queued"}


# ── Header filter ─────────────────────────────────────────────────────────────


def test_extra_headers_are_dropped(mock_sqs: mock.MagicMock) -> None:
    """Non-preserved headers (trace IDs, user-agent, etc.) never enter the envelope."""
    event = _make_event(
        headers={
            "x-github-event": "push",
            "x-github-delivery": "abc-123",
            "x-hub-signature-256": "sha256=cafe",
            "x-amzn-trace-id": "Root=1-deadbeef",
            "user-agent": "GitHub-Hookshot/abc",
            "content-length": "1234",
            "host": "api.example.com",
        },
        body="{}",
    )

    handler_mod.handler(event, None)

    envelope = _envelope_from_call(mock_sqs)
    assert set(envelope["headers"]) == {
        "x-github-event",
        "x-github-delivery",
        "x-hub-signature-256",
    }
    for dropped in ("x-amzn-trace-id", "user-agent", "content-length", "host"):
        assert dropped not in envelope["headers"]


# ── Case normalization ────────────────────────────────────────────────────────


def test_mixed_case_headers_lowercased(mock_sqs: mock.MagicMock) -> None:
    """Mixed-case header names arrive lowercase; the poller filters on lowercase."""
    event = _make_event(
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=feedface",
            "x-github-delivery": "delivery-uuid-here",
            "Content-Type": "application/json",  # dropped, also confirms filter still works
        },
        body="{}",
    )

    handler_mod.handler(event, None)

    envelope = _envelope_from_call(mock_sqs)
    assert envelope["headers"] == {
        "x-github-event": "pull_request",
        "x-hub-signature-256": "sha256=feedface",
        "x-github-delivery": "delivery-uuid-here",
    }
    # The filter operates on the lowercased name, but the keys stored in
    # the envelope must themselves be lowercase — no Pascal-Case keys.
    for key in envelope["headers"]:
        assert key == key.lower()


# ── Missing body ──────────────────────────────────────────────────────────────


def test_missing_body_becomes_empty_string(mock_sqs: mock.MagicMock) -> None:
    """``body=None`` becomes ``""`` — the handler does not crash."""
    event = _make_event(
        headers={"x-github-event": "ping"},
        body=None,
    )

    result = handler_mod.handler(event, None)

    envelope = _envelope_from_call(mock_sqs)
    assert envelope["body"] == ""
    assert result == {"statusCode": 202, "body": "queued"}


# ── Missing headers ───────────────────────────────────────────────────────────


def test_missing_headers_becomes_empty_dict(mock_sqs: mock.MagicMock) -> None:
    """``headers=None`` becomes ``{}`` — the handler does not crash."""
    event = _make_event(headers=None, body="{}")

    result = handler_mod.handler(event, None)

    envelope = _envelope_from_call(mock_sqs)
    assert envelope["headers"] == {}
    assert envelope["body"] == "{}"
    assert result == {"statusCode": 202, "body": "queued"}


# ── Base64-encoded body path ──────────────────────────────────────────────────


def test_base64_encoded_body_decoded(mock_sqs: mock.MagicMock) -> None:
    """``isBase64Encoded=True`` decodes the body to its raw UTF-8 string."""
    raw = json.dumps({"hello": "world", "emoji": "✓"})  # noqa: RUF001
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    event = _make_event(
        headers={"x-hub-signature-256": "sha256=abc"},
        body=encoded,
        is_base64_encoded=True,
    )

    handler_mod.handler(event, None)

    envelope = _envelope_from_call(mock_sqs)
    assert envelope["body"] == raw


def test_base64_encoded_invalid_passes_through(mock_sqs: mock.MagicMock) -> None:
    """Undecodable base64 is passed through unchanged.

    Per ADR-0017: if decode fails, swallow the exception and pass the
    base64 string through — the poller's HMAC check will then fail, the
    message DLQs, and an operator inspects.
    """
    not_base64 = "@@@not-base64@@@"
    event = _make_event(
        headers={"x-github-event": "ping"},
        body=not_base64,
        is_base64_encoded=True,
    )

    result = handler_mod.handler(event, None)

    envelope = _envelope_from_call(mock_sqs)
    assert envelope["body"] == not_base64
    assert result == {"statusCode": 202, "body": "queued"}


# ── Return value ──────────────────────────────────────────────────────────────


def test_return_value_is_queued_202(mock_sqs: mock.MagicMock) -> None:
    """The handler always returns ``{"statusCode": 202, "body": "queued"}``."""
    event = _make_event(
        headers={"x-github-event": "ping"},
        body="{}",
    )

    result = handler_mod.handler(event, None)

    assert result == {"statusCode": 202, "body": "queued"}
