"""Per-host consumer for bound-label messages (ADR-0093).

Reads from the central event log all messages addressed to labels bound to
this host, delivers each via the claim→handle→commit cycle, and wakes the
local session via cc-relay — INTRA-HOST ONLY.

ISOLATION INVARIANT: ``run_once`` calls ``labels_on_host(this_host)`` before
any log read. The ``read_for_labels`` query is filtered to those labels only.
``relay_fn`` is called with those same labels only. No other host's relay
directory is ever written.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from treadmill_api.messaging.consumer import DEFAULT_CRASH_WINDOW, MessageConsumer
from treadmill_api.messaging.dedup import DedupBackend

logger = logging.getLogger(__name__)


def _write_relay(label: str, message: dict[str, Any]) -> None:
    """Write message to cc-relay inbox for label (intra-host wake only)."""
    relay_dir = Path.home() / ".cc-channels" / label / "relay"
    relay_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
    (relay_dir / fname).write_text(json.dumps(message), encoding="utf-8")


class HostConsumer(MessageConsumer):
    """Delivers event log messages to the labels bound to this host.

    Args:
        host: Name of the host this consumer runs on.
        host_store: Has ``labels_on_host(session, host) -> list[str]``.
        log_store: Has ``read_for_labels(session, labels, after_offset, limit)``.
        dedup: DedupBackend scoped to this consumer's identity.
        relay_fn: Called with ``(label, message)`` to wake the local session.
            Defaults to writing a relay file in ``~/.cc-channels/<label>/relay/``.
        crash_window: Claim reservation window (default 5 minutes).
    """

    def __init__(
        self,
        host: str,
        host_store: Any,
        log_store: Any,
        dedup: DedupBackend,
        relay_fn: Callable[[str, dict[str, Any]], None] | None = None,
        crash_window: timedelta = DEFAULT_CRASH_WINDOW,
    ) -> None:
        super().__init__(dedup, crash_window)
        self._host = host
        self._host_store = host_store
        self._log_store = log_store
        self._relay_fn = relay_fn if relay_fn is not None else _write_relay
        self._cursor: int = 0

    def handle(self, message: dict[str, Any]) -> None:
        """Wake the local session for ordering_key via cc-relay (intra-host only)."""
        label = message["ordering_key"]
        self._relay_fn(label, message)

    async def run_once(self, session: Any) -> int:
        """Fetch pending messages for this host's labels and deliver each.

        Queries ``labels_on_host(this_host)`` first. Returns 0 immediately if
        no labels are bound. Otherwise reads the event log after the in-memory
        cursor and delivers each message via the claim→handle→commit cycle.

        Returns the count of rows fetched (including deduplicated rows).
        Advances the in-memory cursor past the highest offset processed.
        """
        labels = await self._host_store.labels_on_host(session, self._host)
        if not labels:
            return 0
        rows = await self._log_store.read_for_labels(
            session, labels, after_offset=self._cursor
        )
        for row in rows:
            msg: dict[str, Any] = {
                "dedupKey": row.dedup_key,
                "ordering_key": row.ordering_key,
                "event_type": row.event_type,
                "payload": row.payload,
            }
            self.deliver(msg)
            if row.offset > self._cursor:
                self._cursor = row.offset
        return len(rows)
