"""Imperative "send an attributed message" client for the exec_otp fabric ingress (#235).

Sibling of ``fabric_event_sink.py``: where the sink pushes lifecycle EVENTS (rendered
``source=ingress``), this sends inter-agent MESSAGES that the ingress renders as
``source=agent:<sender>`` — an attributed, reply-able coordinator↔worker message (the
transport that replaces treadmill's ``cc-relay.py`` briefs, #174).

Two deliberate differences from the event sink:
  * IMPERATIVE, request/response — ``send`` returns success/failure so a dropped brief is
    visible to the caller (a coordinator that briefs a worker must know if it was lost),
    unlike the sink's fire-and-forget-over-the-eventbus. It does NOT swallow a non-2xx.
  * AUTHENTICATED — every POST carries ``Authorization: Bearer <FABRIC_INGRESS_TOKEN>``.
    The #236 ingress is fail-closed: an unauthenticated POST is 401'd.

Security (ADR-0019 / #235):
  * Sender MINTING is the CALLER's responsibility (route-through-API): ``sender`` MUST be
    minted server-side from a TRUSTED value (e.g. a plan/repo → ``coordinator_label``
    lookup, as ``fabric_event_sink._resolve_coordinator_label`` does), NEVER a
    caller-asserted field. This client validates ``sender`` against the treadmill-team
    namespace as defense-in-depth (mirroring the ingress's ``\\A..\\z`` gate), but it
    cannot authenticate the caller — inbound internal-caller auth does not exist in the
    API today and is a scoped PREREQUISITE for the full worker-identity mint.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 5.0

# Mirror the ingress namespace gate (exec_otp ingress.ex @treadmill_sender_re): the ingress mints
# source=agent:<sender> ONLY for a treadmill-team label, with STRICT string anchors \A..\z (a trailing
# newline must not slip a fake header line into the rendered prefix). Reject a bad sender here — the
# caller's first line of defense — rather than relying only on the server.
_TREADMILL_SENDER_RE = re.compile(r"\A(coordinator|worker|evaluator)-[a-z0-9-]{1,60}\Z")


def _now_epoch_seconds() -> int:
    """Unix seconds — the ingress message route's ``ts`` freshness gate expects an integer."""
    return int(datetime.now(timezone.utc).timestamp())


class FabricSend:
    """POSTs an attributed message to the exec_otp fabric ingress message route.

    Dark by default: with no ingress URL OR no token, ``is_configured`` is False and
    ``send`` returns False without a network call (the ingress is fail-closed, so a URL
    without a token could only ever 401). Mirrors the event sink's dark-by-default.
    """

    def __init__(
        self,
        *,
        ingress_url: str | None = None,
        token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # Normalize empty-string -> None so FABRIC_INGRESS_URL="" behaves as unset (dark).
        self.ingress_url = ingress_url or None
        self._token = token or None
        # An injected client (tests, shared) is left alone on shutdown; an owned client is
        # built lazily on start and closed in stop.
        self._injected_client = http_client
        self._http_client: httpx.AsyncClient | None = http_client
        self._timeout_seconds = timeout_seconds

    @property
    def is_configured(self) -> bool:
        """True iff BOTH an ingress URL and a bearer token are set."""
        return bool(self.ingress_url) and bool(self._token)

    async def start(self) -> None:
        """Build the owned http client (with the bearer header). No-op when dark."""
        if not self.is_configured:
            logger.info("fabric send: FABRIC_INGRESS_URL/FABRIC_INGRESS_TOKEN unset; client is dark")
            return
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                headers={"Authorization": f"Bearer {self._token}"},
            )

    async def stop(self) -> None:
        """Close the owned client; an injected one is the caller's. Safe if never started."""
        if self._injected_client is None and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    @staticmethod
    def valid_sender(sender: Any) -> bool:
        """Defense-in-depth mirror of the ingress gate: a treadmill-team label only."""
        return isinstance(sender, str) and _TREADMILL_SENDER_RE.match(sender) is not None

    async def send(self, *, to: str, sender: str, text: str) -> bool:
        """POST an attributed message; return True on a 2xx ingress ack, else False (logged).

        Request/response (unlike the fire-and-forget sink) so a dropped brief is visible.
        ``sender`` MUST be a server-minted treadmill-team label (see module docstring).
        """
        if not self.is_configured or self._http_client is None:
            logger.warning("fabric send: not configured/started; dropping message to %s", to)
            return False
        if not self.valid_sender(sender):
            # A non-treadmill sender must never reach the ingress — the caller minted a bad label.
            logger.error("fabric send: refusing non-treadmill sender %r (to=%s)", sender, to)
            return False
        if not isinstance(to, str) or not to or not isinstance(text, str) or not text:
            logger.error("fabric send: missing/invalid to or text (to=%r, sender=%s)", to, sender)
            return False

        body = {
            "kind": "message",
            "to": to,
            "sender": sender,
            "text": text,
            "ts": _now_epoch_seconds(),
        }
        try:
            resp = await self._http_client.post(self.ingress_url, json=body)
        except Exception:
            logger.exception(
                "fabric send: POST to ingress failed (to=%s, sender=%s); message NOT delivered",
                to,
                sender,
            )
            return False

        if resp.status_code // 100 == 2:
            return True
        logger.error(
            "fabric send: ingress rejected message (to=%s, sender=%s, status=%s); NOT delivered",
            to,
            sender,
            resp.status_code,
        )
        return False


def make_fabric_send(settings: Any) -> FabricSend:
    """Build a ``FabricSend`` from a ``Settings`` instance (mirrors ``make_fabric_event_sink``)."""
    return FabricSend(
        ingress_url=settings.fabric_ingress_url,
        token=getattr(settings, "fabric_ingress_token", None),
    )
