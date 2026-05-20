"""HMAC-SHA256 signature verification for GitHub webhooks.

Cribbed from bunkhouse's webhooks.py contract: GitHub signs the request
body with a shared secret and sends the result in the
``X-Hub-Signature-256`` header. We recompute the HMAC and compare in
constant time. An empty secret (``None`` or empty string) is the
local-dev-only short-circuit per ADR-0007 — production rejects unverified
payloads.
"""

from __future__ import annotations

import hashlib
import hmac


class SignatureMissingError(ValueError):
    """The X-Hub-Signature-256 header was absent (or empty) but a secret
    was configured."""


class InvalidSignatureError(ValueError):
    """The signature header was present but did not match the recomputed
    HMAC. Could indicate tampering or a misconfigured secret."""


def verify_github_signature(
    secret: str | None,
    body: bytes,
    signature_header: str | None,
) -> None:
    """Verify a GitHub webhook signature.

    Returns ``None`` on success. Raises ``SignatureMissingError`` if the
    signature header is missing while a secret is configured;
    ``InvalidSignatureError`` if the header is present but doesn't match.

    When ``secret`` is ``None`` or empty, verification is skipped entirely
    — the local-dev escape hatch. Production deploys must set the secret.
    """
    if not secret:
        return  # dev-mode short-circuit

    if not signature_header:
        raise SignatureMissingError(
            "X-Hub-Signature-256 header is required when GITHUB_WEBHOOK_SECRET is set"
        )

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise InvalidSignatureError("X-Hub-Signature-256 does not match recomputed HMAC")


def verify_github_signature_any(
    secrets: list[str | None],
    body: bytes,
    signature_header: str | None,
) -> None:
    """Verify a GitHub webhook signature against any of several candidate secrets.

    Returns ``None`` if ``verify_github_signature`` succeeds for any non-empty
    secret in ``secrets``. Raises ``InvalidSignatureError`` if none match.

    If ``secrets`` contains only ``None``/empty entries, verification is
    skipped entirely (the local-dev short-circuit), matching the
    single-secret behavior.

    Intended for the App-secret cutover: callers can pass both the legacy
    webhook secret and the GitHub App's webhook secret so a payload signed
    with either is accepted during the transition.
    """
    non_empty = [s for s in secrets if s]
    if not non_empty:
        return  # dev-mode short-circuit — no real secret configured

    for secret in non_empty:
        try:
            verify_github_signature(secret, body, signature_header)
            return
        except (SignatureMissingError, InvalidSignatureError):
            continue

    raise InvalidSignatureError(
        "X-Hub-Signature-256 does not match any configured webhook secret"
    )
