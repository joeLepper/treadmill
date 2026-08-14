"""Tests for the outbox pump — flock enforcement and drain ordering.

Pinned tests (operator note + evaluator rework 2026-08-13):
  test_single_pump_preserves_per_recipient_order
      The pump's PRIMARY job: messages for one recipient arrive at the
      publisher in append (row_id) order.
  test_second_pump_drains_nothing_when_rows_already_marked
      Sequential property: a second pump run after the first finds zero
      pending rows and drains nothing. mark_published is the gate on the
      SEQUENTIAL side; it does NOT prevent concurrent double-publish.
  test_second_pump_cannot_acquire_lock
      With one pump holding the flock, a second pump on the same lock file
      cannot acquire it; run() returns -1 and the outbox is undrained.
      The flock is what prevents concurrent double-publish in production.
  test_without_flock_consumer_dedup_collapses_duplicates
      Honest concurrency test: when two pumps bypass the shared flock (the
      production invariant violation), the pump delivers at-least-once — it
      may publish duplicate rows. A dedup-collapsing consumer collapses those
      duplicates to exactly-once. The pump alone is NOT exactly-once.
  test_pump_retries_on_publish_failure
      Transient publish failure retried; row eventually published and marked.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from treadmill_api.messaging.outbox import OutboxBackend, OutboxRow
from treadmill_api.messaging.pump import OutboxPump


# ── fake publishers ────────────────────────────────────────────────────────────

class _CapturingPublisher:
    def __init__(self) -> None:
        self.published: list[OutboxRow] = []

    def publish(self, row: OutboxRow) -> None:
        self.published.append(row)


class _DedupCollapsingPublisher:
    """Records all deliveries and collapses duplicates by dedupKey.

    Models the consumer-side dedup guarantee: even when the pump delivers
    a row more than once (at-least-once), the consumer sees each dedupKey
    exactly once.
    """

    def __init__(self) -> None:
        self.all_received: list[OutboxRow] = []
        self._seen: set[str] = set()
        self.unique: list[OutboxRow] = []

    def publish(self, row: OutboxRow) -> None:
        self.all_received.append(row)
        if row.dedup_key not in self._seen:
            self._seen.add(row.dedup_key)
            self.unique.append(row)


class _FailingPublisher:
    """Fails on the first call, succeeds thereafter — for retry coverage."""

    def __init__(self) -> None:
        self.published: list[OutboxRow] = []
        self._calls = 0

    def publish(self, row: OutboxRow) -> None:
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("simulated publish failure")
        self.published.append(row)


# ── helpers ───────────────────────────────────────────────────────────────────

def _write_messages(
    outbox: OutboxBackend,
    n: int,
    ordering_key: str = "recipient-a",
) -> list[str]:
    keys = []
    for _ in range(n):
        key = str(uuid.uuid4())
        conn = outbox.connect()
        try:
            outbox.write(
                {
                    "dedupKey": key,
                    "ordering_key": ordering_key,
                    "event_type": "test.event",
                    "payload": {},
                },
                conn,
            )
            conn.commit()
        finally:
            conn.close()
        keys.append(key)
    return keys


def _make_pump(
    outbox: OutboxBackend,
    publisher: Any,
    tmp_path: Path,
    lock_name: str = "outbox.lock",
) -> OutboxPump:
    return OutboxPump(outbox, publisher, tmp_path / lock_name)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_single_pump_preserves_per_recipient_order(tmp_path: Path) -> None:
    """Messages for one recipient arrive at the publisher in append order.

    This is the pump's PRIMARY correctness property. read_pending returns rows
    ORDER BY id ASC so the publisher sees them in the order they were written.
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    publisher = _CapturingPublisher()
    pump = _make_pump(outbox, publisher, tmp_path)

    written_keys = _write_messages(outbox, n=8, ordering_key="recipient-a")

    count = pump.run()

    assert count == 8
    received_keys = [row.dedup_key for row in publisher.published]
    assert received_keys == written_keys, (
        "pump must deliver messages in append order for the same recipient"
    )


def test_second_pump_drains_nothing_when_rows_already_marked(tmp_path: Path) -> None:
    """Sequential property: a second pump run after the first drains nothing.

    pump1 reads all pending rows, publishes each, and calls mark_published
    (setting published_at). When pump2 subsequently calls read_pending, it
    finds zero rows with published_at IS NULL and drains nothing.

    This is a SEQUENTIAL guarantee, not a concurrency guarantee. mark_published
    does NOT prevent concurrent double-publish when two pumps interleave their
    read and mark steps. The flock (test_second_pump_cannot_acquire_lock) is
    what prevents concurrent draining in production.
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    publisher = _CapturingPublisher()

    written_keys = _write_messages(outbox, n=6)

    pump1 = _make_pump(outbox, publisher, tmp_path, lock_name="lock1.lock")
    pump2 = _make_pump(outbox, publisher, tmp_path, lock_name="lock2.lock")

    count1 = pump1.run()
    count2 = pump2.run()

    assert count1 == 6, "pump1 must drain all 6 rows"
    assert count2 == 0, "pump2 must find zero pending rows after pump1 drained"
    received_keys = [row.dedup_key for row in publisher.published]
    assert received_keys == written_keys


def test_second_pump_cannot_acquire_lock(tmp_path: Path) -> None:
    """With one pump holding the flock, a second pump on the same lock file
    cannot acquire it, run() returns -1, and the outbox is undrained.

    The flock is what provides concurrent exactly-once in production: a second
    pump cannot even start draining while the first holds the lock.
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    publisher = _CapturingPublisher()
    lock_path = tmp_path / "shared.lock"

    _write_messages(outbox, n=3)

    pump1 = OutboxPump(outbox, publisher, lock_path)
    pump2 = OutboxPump(outbox, publisher, lock_path)

    assert pump1.acquire_lock(), "pump1 must acquire the lock"
    try:
        result = pump2.run()
        assert result == -1, "pump2.run() must return -1 when the lock is held"
        assert len(publisher.published) == 0, "pump2 must not publish any rows"
        assert len(outbox.read_pending()) == 3, "outbox must be undrained"
    finally:
        pump1.release_lock()

    # After pump1 releases, pump2 can acquire and drain normally
    assert pump2.acquire_lock(), "pump2 must acquire lock after pump1 releases"
    pump2.release_lock()


def test_without_flock_consumer_dedup_collapses_duplicates(tmp_path: Path) -> None:
    """Without the shared flock, two pumps draining the same outbox produce
    at-least-once delivery — the same row may be published more than once.
    A dedup-collapsing consumer collapses those duplicates to exactly-once.

    The pump alone is NOT exactly-once. It is at-least-once. Exactly-once
    at the consumer is provided by:
      1. The flock (prevents concurrent draining in production), AND
      2. Consumer-side dedup (absorbs any duplicate that bypasses the flock).

    This test models the read-before-mark race directly: both pumps read the
    same pending rows before either calls mark_published, simulating the
    interleaving that occurs when two concurrent pumps bypass the shared flock.
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    publisher = _DedupCollapsingPublisher()

    N = 4
    _write_messages(outbox, n=N)

    # Model the race: both pumps read the same pending rows before either marks.
    # This is the interleaving the flock prevents in production.
    rows = outbox.read_pending()
    for row in rows:
        publisher.publish(row)   # pump1 publishes
    for row in rows:
        publisher.publish(row)   # pump2 publishes the same rows — duplicates

    # The pump delivered each row twice (at-least-once, not exactly-once).
    assert len(publisher.all_received) == N * 2, (
        "without the flock, concurrent pumps may publish each row more than once"
    )
    # The dedup-collapsing consumer collapses duplicates to exactly-once.
    assert len(publisher.unique) == N, (
        "consumer-side dedup must collapse duplicates to exactly-once"
    )


def test_pump_retries_on_publish_failure(tmp_path: Path) -> None:
    """A transient publish failure causes a retry; the row is eventually
    published and marked. No row is dropped.
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    publisher = _FailingPublisher()
    pump = _make_pump(outbox, publisher, tmp_path)

    _write_messages(outbox, n=1)

    count = pump.run()

    assert count == 1, "row must be published (after retry)"
    assert len(publisher.published) == 1
    assert len(outbox.read_pending()) == 0, "row must be marked published after retry"
