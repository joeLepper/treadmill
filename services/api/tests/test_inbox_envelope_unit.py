"""Unit tests for ``WebhookInboxEnvelope`` — the AWS->local boundary type.

The model is the Lambda <-> poller contract from ADR-0017. These tests
lock down: round-trip JSON encode/decode, the three required-field
behaviors (``headers``, ``body``, no extras), Pydantic's type coercion
posture at this boundary, and a realistic GitHub ``pull_request:opened``
shape.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from treadmill_api.webhooks.inbox_envelope import WebhookInboxEnvelope


def test_round_trip_minimal_envelope():
    """``model_validate_json`` -> ``model_dump_json`` is shape-stable."""
    raw = {
        "headers": {
            "x-github-event": "pull_request",
            "x-github-delivery": "12345678-1234-1234-1234-123456789abc",
            "x-hub-signature-256": "sha256=deadbeef",
        },
        "body": '{"action":"opened"}',
    }
    envelope = WebhookInboxEnvelope.model_validate_json(json.dumps(raw))

    assert envelope.headers == raw["headers"]
    assert envelope.body == raw["body"]

    # Round-trip: dump back to JSON, reparse, and compare on the dict.
    reparsed = json.loads(envelope.model_dump_json())
    assert reparsed == raw


def test_missing_headers_field_raises():
    with pytest.raises(ValidationError):
        WebhookInboxEnvelope.model_validate_json(json.dumps({"body": ""}))


def test_missing_body_field_raises():
    with pytest.raises(ValidationError):
        WebhookInboxEnvelope.model_validate_json(json.dumps({"headers": {}}))


def test_extra_field_raises_per_forbid_config():
    """``extra="forbid"`` rejects unknown top-level keys — the discipline
    that catches contract drift between the Lambda writer and poller
    reader at the boundary, not three layers in."""
    payload = {"headers": {}, "body": "", "extra": "foo"}
    with pytest.raises(ValidationError):
        WebhookInboxEnvelope.model_validate_json(json.dumps(payload))


def test_wrong_type_for_headers_raises():
    """``headers`` must be an object, not a string. Pydantic rejects."""
    payload = {"headers": "not a dict", "body": ""}
    with pytest.raises(ValidationError):
        WebhookInboxEnvelope.model_validate_json(json.dumps(payload))


def test_wrong_type_for_body_raises():
    """``body`` is typed as ``str``. Pydantic v2's default JSON mode does
    not coerce a JSON number into a string — lock that in so a malformed
    Lambda write surfaces at the boundary, not at the HMAC step downstream
    (which would attempt ``.encode("utf-8")`` on a non-str)."""
    payload = {"headers": {}, "body": 42}
    with pytest.raises(ValidationError):
        WebhookInboxEnvelope.model_validate_json(json.dumps(payload))


def test_empty_values_validate():
    """No minimum-length constraint on either field — the model only
    asserts shape, not content. The poller's own header-presence checks
    (per ADR-0017's header preservation contract) handle empty-headers
    rejection; signature verification handles empty-body rejection."""
    envelope = WebhookInboxEnvelope.model_validate_json(
        json.dumps({"headers": {}, "body": ""})
    )
    assert envelope.headers == {}
    assert envelope.body == ""


def test_realistic_pull_request_opened_envelope_round_trips():
    """A representative GitHub ``pull_request:opened`` delivery: the three
    load-bearing headers per ADR-0017's header-preservation contract plus
    a JSON body of roughly the right shape and size (~hundreds of bytes).
    """
    body_obj = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Add webhook inbox envelope model",
            "head": {
                "ref": "feat/inbox-envelope",
                "sha": "1234567890abcdef1234567890abcdef12345678",
            },
            "base": {
                "ref": "main",
                "sha": "abcdef1234567890abcdef1234567890abcdef12",
            },
            "user": {"login": "octocat"},
            "merged": False,
        },
        "repository": {"full_name": "anthropic-experimental/treadmill"},
        "sender": {"login": "octocat"},
    }
    raw = {
        "headers": {
            "x-github-event": "pull_request",
            "x-github-delivery": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
            "x-hub-signature-256": (
                "sha256="
                "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"
            ),
        },
        "body": json.dumps(body_obj),
    }

    envelope = WebhookInboxEnvelope.model_validate_json(json.dumps(raw))
    assert envelope.headers["x-github-event"] == "pull_request"
    assert envelope.headers["x-github-delivery"].count("-") == 4
    assert envelope.headers["x-hub-signature-256"].startswith("sha256=")

    # The body is the *string* form — the poller passes
    # ``envelope.body.encode("utf-8")`` to ``verify_github_signature``,
    # so byte-stability of this string is load-bearing.
    assert isinstance(envelope.body, str)
    assert json.loads(envelope.body) == body_obj
    # Sanity: representative blob is hundreds of bytes, not pathologically small.
    assert 200 < len(envelope.body) < 2000

    # Round-trip stability through model_dump_json.
    reparsed = json.loads(envelope.model_dump_json())
    assert reparsed == raw
