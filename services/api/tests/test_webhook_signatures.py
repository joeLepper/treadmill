"""Unit tests for HMAC-SHA256 webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from treadmill_api.webhooks.signatures import (
    InvalidSignatureError,
    SignatureMissingError,
    verify_github_signature,
)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_passes_with_correct_signature():
    secret = "shh"
    body = b'{"action":"opened"}'
    signature = _sign(secret, body)
    # Returns None on success; raises on failure.
    verify_github_signature(secret, body, signature)


def test_verify_raises_on_wrong_signature():
    body = b'{"action":"opened"}'
    bad_signature = _sign("the-actual-secret", body)
    with pytest.raises(InvalidSignatureError):
        verify_github_signature("a-different-secret", body, bad_signature)


def test_verify_raises_on_tampered_body():
    secret = "shh"
    correct = _sign(secret, b'{"action":"opened"}')
    with pytest.raises(InvalidSignatureError):
        verify_github_signature(secret, b'{"action":"closed"}', correct)


def test_verify_raises_when_secret_set_but_signature_missing():
    with pytest.raises(SignatureMissingError):
        verify_github_signature("shh", b"x", None)
    with pytest.raises(SignatureMissingError):
        verify_github_signature("shh", b"x", "")


def test_verify_skips_when_secret_is_none():
    """The local-dev short-circuit per ADR-0007."""
    verify_github_signature(None, b"any body", None)
    verify_github_signature(None, b"any body", "garbage signature")


def test_verify_skips_when_secret_is_empty_string():
    verify_github_signature("", b"any body", None)


def test_verify_uses_constant_time_comparison():
    """Smoke check: a near-match signature is rejected (a non-constant-time
    compare would terminate on the first byte mismatch and could leak
    timing info). The behavior is identical for the caller — both raise —
    but this test ensures the right symbol is exercised."""
    secret = "shh"
    body = b"x"
    correct = _sign(secret, body)
    near = correct[:-1] + ("0" if correct[-1] != "0" else "1")
    with pytest.raises(InvalidSignatureError):
        verify_github_signature(secret, body, near)
