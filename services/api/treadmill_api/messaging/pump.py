"""Single flock-enforced outbox pump (ADR-0094).

Drains pending outbox rows to the central event log in per-recipient
(ordering_key) append order. The pump's PRIMARY job is preserving order;
ADR-0093 dedup makes a stray second pump non-corrupting (consumers drop
duplicates on dedupKey) but ordering is lost without the flock.

Invariants — both must hold:

1. Exactly one active pump per outbox, enforced by flock on a sidecar lock
   file. The flock is acquired with LOCK_EX | LOCK_NB (non-blocking) at pump
   startup and held for the pump's lifetime. It is released automatically on
   process death (no stale pidfile risk). A pump that cannot acquire the lock
   must not drain.

2. Exactly one active consumer per consumer_id at any time. DedupBackend.claim()
   is SELECT-then-INSERT/UPDATE and is safe only when one consumer per
   consumer_id runs concurrently. Because this pump is the sole active consumer
   of each outbox, and the flock ensures exactly one pump is active, invariant 2
   is satisfied by invariant 1. Never bypass the flock and run two pumps against
   the same outbox.
"""

from __future__ import annotations

import fcntl
import logging
from pathlib import Path
from typing import IO, Any, Protocol

from treadmill_api.messaging.outbox import OutboxBackend, OutboxRow

logger = logging.getLogger(__name__)


class RowPublisher(Protocol):
    """Publish a single outbox row to the central event log.

    Implementations must be synchronous so the drain loop can retry inline.
    For async publishers (e.g. SNS), wrap with asyncio.run() or run in a
    dedicated thread.
    """

    def publish(self, row: OutboxRow) -> None: ...


class OutboxPump:
    """Flock-enforced pump that drains an outbox to the event log.

    Args:
        outbox: The outbox to drain.
        publisher: Injected publisher. Must raise on failure so the drain loop
            retries. Tests inject a capturing fake; production wires in a
            wrapper over the SNS EventPublisher.
        lock_path: Sidecar lock file path. Use a path beside the outbox DB
            (e.g. db_path.with_suffix(".lock")). Using a sidecar avoids
            interfering with SQLite's own file-locking.
        batch_size: Rows per read_pending call.
    """

    def __init__(
        self,
        outbox: OutboxBackend,
        publisher: RowPublisher,
        lock_path: Path,
        batch_size: int = 100,
    ) -> None:
        self._outbox = outbox
        self._publisher = publisher
        self._lock_path = lock_path
        self._batch_size = batch_size
        self._lock_file: IO[Any] | None = None

    def acquire_lock(self) -> bool:
        """Attempt a non-blocking exclusive flock on the lock file.

        Returns True if the lock is now held by this pump. Returns False if
        another pump already holds it — the caller must not drain.
        """
        f = None
        try:
            f = open(self._lock_path, "w")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file = f
            return True
        except (BlockingIOError, OSError):
            if f is not None:
                f.close()
            return False

    def release_lock(self) -> None:
        """Release the flock and close the lock file descriptor."""
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def drain(self) -> int:
        """Drain all pending outbox rows in append order.

        For each row: publish → mark_published. Retries publish indefinitely
        on failure; never drops a row. Returns the count of rows published.
        """
        total = 0
        while True:
            rows = self._outbox.read_pending(limit=self._batch_size)
            if not rows:
                break
            for row in rows:
                self._publish_with_retry(row)
                self._outbox.mark_published(row.row_id)
                total += 1
        return total

    def _publish_with_retry(self, row: OutboxRow) -> None:
        while True:
            try:
                self._publisher.publish(row)
                return
            except Exception:
                logger.exception(
                    "pump: publish failed for outbox row %d; retrying", row.row_id
                )

    def run(self) -> int:
        """Acquire lock and drain. Returns -1 if the lock is not acquired.

        The flock is released when run() returns, regardless of outcome.
        """
        if not self.acquire_lock():
            logger.info("pump: flock not acquired — another pump is running; skipping drain")
            return -1
        try:
            return self.drain()
        finally:
            self.release_lock()

    def __enter__(self) -> "OutboxPump":
        return self

    def __exit__(self, *_: object) -> None:
        self.release_lock()
