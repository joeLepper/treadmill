"""Notification fan-out for escalation events (ADR-0062 Step 4).

This module owns the out-of-process notification side of the escalation
incident lifecycle. The deterministic open + close events
(``task.escalated_to_operator``, ``task.escalation_closed`` — see Steps
1-3) already land in the audit log and on the operator dashboard; this
subscriber pushes the same signal to the channels operators actually
watch (Slack today, arbitrary HTTP endpoints in general).

Design — why subscribe in-process rather than a fresh SQS queue?

The eventbus already exposes ``subscribe_local`` / ``unsubscribe_local``
(used by the dashboard's WebSocket fan-out, ADR-0056). Every publish
fans through the in-process broadcaster *before* the SNS hop, so a
local subscriber sees the same record SNS would have carried, with no
extra AWS plumbing and no per-deployment queue + subscription filter to
wire. The trade-off — fan-out is per-API-process, not durable across a
restart — is the right one for "best-effort operator notification":
delivery loss on restart is acceptable; an operator who needs the
authoritative trail reads the events table.

Wire shape per target
---------------------

  * ``TREADMILL_SLACK_WEBHOOK_URL`` — single Slack incoming-webhook URL.
    On every escalation open / close we POST a Slack-formatted body:
    ``{"text": "<emoji> Task <short-id> escalation <verb> — reason=<r>"
    [+ " mttr=<n>s" on close]}``. The format is intentionally one line
    so the channel digest stays readable.

  * ``TREADMILL_NOTIFICATION_WEBHOOKS`` — comma-separated list of generic
    webhook URLs. Each receives a POST whose body is the raw typed-event
    record (the same dict ``eventbus._build_record`` produces — event_id,
    entity_type, action, plan/task/run/step ids, and the encoded payload).
    Downstream consumers (PagerDuty bridges, custom incident routers,
    etc.) parse that shape directly; no Slack-specific wrapping leaks
    into the generic surface.

  * ``TREADMILL_TELEGRAM_BOT_TOKEN`` + ``TREADMILL_TELEGRAM_CHAT_ID``
    (ADR-0071) — when BOTH are set, every escalation open / close POSTs
    ``{chat_id, text}`` to ``https://api.telegram.org/bot<token>/sendMessage``
    where ``text`` is the same one-line summary as the Slack body without
    Slack's emoji syntax (plain words, no ``:rotating_light:``). Telegram
    is a sibling target alongside Slack — not a replacement; an operator
    runs either or both. The bot token is a secret: read from settings,
    never logged.

Failure isolation invariant
---------------------------

Per the task brief: per-target failures log and continue. A timeout to
Slack does not block the generic-webhook fan-out; one bad URL in the
generic list does not poison the others. The subscriber never raises
into the eventbus broadcaster — a wedged notification path must not
back-pressure the publish loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from treadmill_api.eventbus import subscribe_local, unsubscribe_local

logger = logging.getLogger("treadmill.coordination.notification_fanout")


_DEFAULT_TIMEOUT_SECONDS = 5.0
"""Per-POST timeout. Short enough that a wedged Slack endpoint doesn't
build a backlog on the in-process queue; long enough that a healthy
endpoint responds well inside the budget."""


_SLACK_EMOJI_OPENED = ":rotating_light:"
_SLACK_EMOJI_CLOSED = ":white_check_mark:"


_OPEN_ACTION = "escalated_to_operator"
_CLOSE_ACTION = "escalation_closed"


_TELEGRAM_API_BASE = "https://api.telegram.org"


def _short_task_id(task_id: str | None) -> str:
    """First 8 chars of a UUID, or ``"unknown"`` when missing.

    A short prefix is enough to grep the audit log / dashboard and keeps
    the Slack one-liner narrow. We deliberately don't hash or otherwise
    obscure the value — the channel is operator-only.
    """
    if not task_id:
        return "unknown"
    return task_id[:8]


def _format_slack_body(record: dict[str, Any]) -> dict[str, Any]:
    """Render the one-line Slack payload for an escalation record.

    Open event: ``:rotating_light: Task <id> escalated to operator —
    reason=<r>``. The reason is pulled from the typed payload's ``reason``
    field when present, else ``unknown`` (older emitters predate ADR-0058's
    reason taxonomy).

    Close event: ``:white_check_mark: Task <id> escalation closed —
    reason=<r> mttr=<n>s``. The MTTR is computed and stamped on the close
    event at emit time (see ``coordination/escalation_close_sweep.py``)
    so we never recompute it here.
    """
    action = record.get("action")
    short_id = _short_task_id(record.get("task_id"))
    payload = record.get("payload") or {}

    if action == _OPEN_ACTION:
        reason = payload.get("reason") or "unknown"
        created_by = payload.get("created_by")
        by_clause = f" by {created_by}" if created_by else ""
        text = (
            f"{_SLACK_EMOJI_OPENED} Task {short_id} escalated to operator{by_clause} "
            f"— reason={reason}"
        )
    elif action == _CLOSE_ACTION:
        reason = payload.get("close_reason") or "unknown"
        mttr = payload.get("mttr_seconds")
        mttr_chunk = f" mttr={mttr}s" if mttr is not None else ""
        text = (
            f"{_SLACK_EMOJI_CLOSED} Task {short_id} escalation closed "
            f"— reason={reason}{mttr_chunk}"
        )
    else:
        # Defensive — ``_dispatch`` filters before calling us, but keep
        # the formatter total so a refactor that broadens the gate
        # doesn't produce an unlabelled message.
        text = f"Task {short_id} {action}"
    return {"text": text}


def _format_telegram_text(record: dict[str, Any]) -> str:
    """Render the plain one-line Telegram summary for an escalation record.

    Same content as the Slack body, minus Slack's ``:emoji:`` syntax —
    Telegram clients render the literal ``:rotating_light:`` rather than
    a glyph, so we strip the emoji shortcodes and leave the words.
    """
    action = record.get("action")
    short_id = _short_task_id(record.get("task_id"))
    payload = record.get("payload") or {}

    if action == _OPEN_ACTION:
        reason = payload.get("reason") or "unknown"
        created_by = payload.get("created_by")
        by_clause = f" by {created_by}" if created_by else ""
        return (
            f"Task {short_id} escalated to operator{by_clause} — reason={reason}"
        )
    if action == _CLOSE_ACTION:
        reason = payload.get("close_reason") or "unknown"
        mttr = payload.get("mttr_seconds")
        mttr_chunk = f" mttr={mttr}s" if mttr is not None else ""
        return (
            f"Task {short_id} escalation closed — reason={reason}{mttr_chunk}"
        )
    return f"Task {short_id} {action}"


class NotificationFanout:
    """Background subscriber that POSTs escalation events to webhook targets.

    Constructor takes the parsed config directly (not a ``Settings``
    instance) so tests can drive the subscriber against ad-hoc URLs
    without round-tripping through env-var parsing. The lifespan handler
    in ``app.py`` is the one place that builds it from ``Settings``.

    Lifecycle mirrors ``CoordinationConsumer`` / ``ReplayLoop`` —
    ``start()`` is idempotent on a no-config build (returns immediately
    without spawning a task), and ``stop()`` is safe to call on a
    never-started instance.
    """

    def __init__(
        self,
        *,
        slack_webhook_url: str | None = None,
        raw_webhook_urls: list[str] | None = None,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # Normalize empty-string Slack URL → None so the env-var path
        # ``TREADMILL_SLACK_WEBHOOK_URL=""`` behaves the same as unset.
        self.slack_webhook_url = slack_webhook_url or None
        self.raw_webhook_urls = list(raw_webhook_urls or [])
        # ADR-0071: Telegram target — both token + chat id are required.
        # Either one unset = no Telegram hop.
        self.telegram_bot_token = telegram_bot_token or None
        self.telegram_chat_id = telegram_chat_id or None
        # An injected client (tests, shared with other subsystems) is
        # left alone on shutdown; an owned client is built lazily on
        # start and closed in ``stop``.
        self._injected_client = http_client
        self._http_client: httpx.AsyncClient | None = http_client
        self._timeout_seconds = timeout_seconds
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def is_configured(self) -> bool:
        """True iff at least one target is configured. Lets the lifespan
        handler skip ``start()`` when there's nothing to fan out to."""
        return (
            bool(self.slack_webhook_url)
            or bool(self.raw_webhook_urls)
            or self._telegram_configured
        )

    @property
    def _telegram_configured(self) -> bool:
        """ADR-0071: Telegram requires BOTH the bot token and chat id."""
        return bool(self.telegram_bot_token) and bool(self.telegram_chat_id)

    async def start(self) -> None:
        """Subscribe to the eventbus broadcaster and spin the fan-out loop.

        No-op when no target is configured — the subscription itself is
        free, but allocating a queue + a polling task for zero output
        targets is dead weight, and skipping makes the wiring's intent
        ("fan-out is opt-in") readable at the call site.
        """
        if not self.is_configured:
            logger.info(
                "notification fanout: no slack, webhook, or telegram targets "
                "configured; skipping start"
            )
            return
        self._stopped = False
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout_seconds)
        # Subscribe BEFORE spawning the consumer task so the queue exists
        # by the time the task awaits ``queue.get()``.
        self._queue = subscribe_local()
        self._task = asyncio.create_task(
            self._run(), name="notification-fanout",
        )
        # Telegram bot token is a secret — log only its configured/unconfigured
        # state, never the value itself.
        logger.info(
            "notification fanout started: slack=%s raw_webhooks=%d telegram=%s",
            "configured" if self.slack_webhook_url else "unconfigured",
            len(self.raw_webhook_urls),
            "configured" if self._telegram_configured else "unconfigured",
        )

    async def stop(self) -> None:
        """Tear down the loop + unsubscribe + close the owned http client.

        Safe to call on a never-started instance (no-target build): the
        early returns mirror the no-op start path.
        """
        self._stopped = True
        if self._queue is not None:
            unsubscribe_local(self._queue)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "notification fanout raised on shutdown",
                )
            self._task = None
        self._queue = None
        # Only close the client we own; an injected one is the caller's
        # responsibility.
        if self._injected_client is None and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("notification fanout stopped")

    async def _run(self) -> None:
        assert self._queue is not None  # for the type-checker; set in start()
        while not self._stopped:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self.handle(record)
            except Exception:
                # Belt-and-braces — ``handle`` already catches per-target
                # exceptions, but a bug in the gating logic must never
                # take the loop down.
                logger.exception(
                    "notification fanout: handler raised; continuing",
                )

    async def handle(self, record: dict[str, Any]) -> None:
        """Public entry point — exposed so tests can drive the fan-out
        without spinning the asyncio loop.

        Filters to the two escalation verbs (open + close) and dispatches
        to each configured target. Per-target failures are caught + logged
        here, never propagated.
        """
        if record.get("entity_type") != "task":
            return
        action = record.get("action")
        if action not in (_OPEN_ACTION, _CLOSE_ACTION):
            return
        if self._http_client is None:
            # Defensive: ``start()`` builds the client. If we got here
            # without one, the subscriber wasn't started — drop quietly.
            logger.warning(
                "notification fanout: handle called without http client; "
                "skipping record event_id=%s",
                record.get("event_id"),
            )
            return

        # Slack first — it's the operator-facing channel and we want the
        # human signal landing as quickly as the generic webhook signal.
        # All arms are independent: a Slack failure does not skip the
        # raw-webhook or Telegram fan-out, and a downstream failure does
        # not retro-actively poison the earlier ones.
        if self.slack_webhook_url:
            await self._post_slack(record)
        for url in self.raw_webhook_urls:
            await self._post_raw(url, record)
        if self._telegram_configured:
            await self._post_telegram(record)

    async def _post_slack(self, record: dict[str, Any]) -> None:
        assert self._http_client is not None
        body = _format_slack_body(record)
        try:
            await self._http_client.post(
                self.slack_webhook_url,  # type: ignore[arg-type]
                json=body,
            )
        except Exception:
            logger.exception(
                "notification fanout: slack post failed for event_id=%s "
                "(action=%s); continuing",
                record.get("event_id"),
                record.get("action"),
            )

    async def _post_raw(self, url: str, record: dict[str, Any]) -> None:
        assert self._http_client is not None
        try:
            await self._http_client.post(url, json=record)
        except Exception:
            logger.exception(
                "notification fanout: raw webhook post failed url=%s "
                "event_id=%s (action=%s); continuing",
                url,
                record.get("event_id"),
                record.get("action"),
            )

    async def _post_telegram(self, record: dict[str, Any]) -> None:
        assert self._http_client is not None
        assert self.telegram_bot_token is not None
        assert self.telegram_chat_id is not None
        url = f"{_TELEGRAM_API_BASE}/bot{self.telegram_bot_token}/sendMessage"
        body = {
            "chat_id": self.telegram_chat_id,
            "text": _format_telegram_text(record),
        }
        try:
            await self._http_client.post(url, json=body)
        except Exception:
            # Token sits in the URL — log only the action + event_id so
            # we never leak the secret through error paths.
            logger.exception(
                "notification fanout: telegram post failed for "
                "event_id=%s (action=%s); continuing",
                record.get("event_id"),
                record.get("action"),
            )


def make_notification_fanout(settings: Any) -> NotificationFanout:
    """Build a ``NotificationFanout`` from a ``Settings`` instance.

    Pulled out of the lifespan handler so the construction path is
    testable on its own and so the lifespan stays focused on wiring.
    """
    return NotificationFanout(
        slack_webhook_url=settings.slack_webhook_url,
        raw_webhook_urls=settings.notification_webhook_urls,
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
    )
