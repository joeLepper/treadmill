"""Unit tests for the dual-secret HMAC-SHA256 webhook signature verifier.

Covers the App-secret cutover prep: ``verify_github_signature_any``
accepts a payload signed by *any* of several candidate secrets so the
verifier can bridge the legacy webhook secret and the GitHub App's
webhook secret during the transition.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from treadmill_api.webhooks.signatures import (
    InvalidSignatureError,
    verify_github_signature_any,
)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


BODY = b'{"action":"opened"}'
SIG_A = _sign("A", BODY)


def test_single_matching_secret_passes():
    assert verify_github_signature_any(["A"], BODY, SIG_A) is None


def test_second_secret_matches():
    # Signature was made with "A"; "B" fails, "A" succeeds — the verifier
    # should try each non-empty secret and accept on the first match.
    assert verify_github_signature_any(["B", "A"], BODY, SIG_A) is None


def test_no_secret_matches_raises_invalid_signature():
    with pytest.raises(InvalidSignatureError):
        verify_github_signature_any(["B"], BODY, SIG_A)


def test_all_empty_secrets_is_dev_mode_skip():
    # Mirrors the single-secret behavior: when no real secret is configured
    # we short-circuit and skip verification entirely.
    assert verify_github_signature_any([None, ""], BODY, None) is None
