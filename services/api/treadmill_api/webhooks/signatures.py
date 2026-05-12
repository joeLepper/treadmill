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
