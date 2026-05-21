"""Unit tests for the webhook poller's dual-secret verification (ADR-0049 phase 6).

The legacy webhook secret is fetched by name from Secrets Manager (the API's IAM
user can read it); the App webhook secret is injected by the adapter as a value
(the API's IAM user cannot read the manually-created secret), so the poller takes
``app_webhook_secret`` directly.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import MagicMock

import pytest

from treadmill_api.coordination.webhook_inbox import WebhookInboxPoller
from treadmill_api.webhooks.signatures import InvalidSignatureError

_LEGACY_NAME = "treadmill-personal/github-webhook-secret"


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _poller(*, app_secret: str | None = "app-sec") -> WebhookInboxPoller:
    sm = MagicMock()
    sm.get_secret_value.side_effect = lambda SecretId: {"SecretString": "legacy-sec"}
    return WebhookInboxPoller(
        sqs_client=MagicMock(),
        queue_url="q",
        secrets_manager_client=sm,
        webhook_secret_name=_LEGACY_NAME,
        sessionmaker=MagicMock(),
        publisher=MagicMock(),
        app_webhook_secret=app_secret,
    )


@pytest.mark.asyncio
async def test_legacy_fetched_app_injected() -> None:
    p = _poller()
    p._webhook_secret = await p._fetch_webhook_secret()  # by-name fetch (IAM-allowed)
    assert p._webhook_secret == "legacy-sec"
    assert p._app_webhook_secret == "app-sec"  # injected value, no fetch


@pytest.mark.asyncio
async def test_verifies_against_either_secret() -> None:
    p = _poller()
    p._webhook_secret = "legacy-sec"
    body = b'{"action":"opened"}'
    secrets = [p._webhook_secret, p._app_webhook_secret]
    p._verifier(secrets, body, _sig("legacy-sec", body))   # legacy delivery
    p._verifier(secrets, body, _sig("app-sec", body))       # App delivery
    with pytest.raises(InvalidSignatureError):
        p._verifier(secrets, body, _sig("not-a-real-secret", body))


def test_app_secret_optional() -> None:
    assert _poller(app_secret=None)._app_webhook_secret is None
