"""Base consumer implementing the claim→process→commit cycle (ADR-0093).

Effectively-once delivery requires idempotent handlers. A handler that
succeeds but whose process dies before commit will be redelivered and run
again. The consumer provides at-most-one execution per dedup key within
an unexpired crash window, but CANNOT guarantee exactly-once processing
across redeliveries. Handlers MUST be idempotent.

Provenance: the claim/commit cycle follows medicoder ADR-0014.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from treadmill_api.messaging.dedup import ClaimResult, DedupBackend

logger = logging.getLogger(__name__)

DEFAULT_CRASH_WINDOW = timedelta(minutes=5)


class MessageConsumer:
    """Base consumer that wraps handle() in the claim→process→commit cycle.

    Subclass and override handle() to implement message processing logic.
    Handlers MUST be idempotent — a handler that succeeds but whose process
    dies before commit will be redelivered and run again. The consumer does
    not guarantee exactly-once processing; it guarantees effectively-once
    delivery when handlers are idempotent.

    Args:
        dedup: DedupBackend scoped to this consumer's identity.
        crash_window: How long a claim stays reserved after it is granted.
            A process that dies within this window causes the message to
            redeliver after the window expires. Pass timedelta(0) in tests
            to make claims expire immediately.
    """

    def __init__(
        self,
        dedup: DedupBackend,
        crash_window: timedelta = DEFAULT_CRASH_WINDOW,
    ) -> None:
        self._dedup = dedup
        self._crash_window = crash_window

    def deliver(self, message: dict[str, Any]) -> None:
        """Run the claim→handle→commit cycle for one message.

        Does NOT commit if handle() raises — leaves the claim to expire so
        the message is redelivered once the crash window passes.
        """
        dedup_key = message["dedupKey"]
        now = datetime.now(timezone.utc)
        result = self._dedup.claim(dedup_key, now, self._crash_window)
        if result != ClaimResult.GRANTED:
            logger.debug("dedup: skipping message key=%s reason=%s", dedup_key, result)
            return
        self.handle(message)
        self._dedup.commit(dedup_key)

    def handle(self, message: dict[str, Any]) -> None:
        """Process a message. Subclasses must override this method.

        Implementations MUST be idempotent: a successful handle() call may
        run again on redelivery if the process dies before commit().
        """
        raise NotImplementedError
