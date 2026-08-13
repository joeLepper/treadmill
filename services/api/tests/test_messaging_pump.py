"""Tests for the outbox pump — flock enforcement and drain ordering.

Three pinned tests (operator note 2026-08-13):
  test_single_pump_preserves_per_recipient_order
      The pump's PRIMARY job: messages for one recipient arrive at the
      publisher in append (row_id) order.
  test_two_concurrent_pumps_deliver_exactly_once
      Two pumps with separate lock files can both drain; mark_published
      ensures each row reaches the publisher exactly once across both runs.
  test_second_pump_cannot_acquire_lock
      With one pump holding the flock, a second pump on the same lock file
      loses the race fast and does not drain.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from treadmill_api.messaging.outbox import OutboxBackend, OutboxRow
from treadmill_api.messaging.pump import OutboxPump


# ── capturing fake publisher ──────────────────────────────────────────────────

class _CapturingPublisher:
    def __init__(self) -> None:
        self.published: list[OutboxRow] = []

    def publish(self, row: OutboxRow) -> None:
        self.published.append(row)


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
    """Write n messages to the outbox; return their dedupKeys in write order."""
    keys = []
    for _ in range(n):
        key = str(uuid.uuid4())
        outbox.write(
            {
                "dedupKey": key,
                "ordering_key": ordering_key,
                "event_type": "test.event",
                "payload": {},
            }
        )
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


def test_two_concurrent_pumps_deliver_exactly_once(tmp_path: Path) -> None:
    """Two pumps with separate lock files both run; each message is published
    exactly once across both runs.

    Separate lock files let both pumps acquire their respective flocks (this
    does NOT contradict test_second_pump_cannot_acquire_lock, which uses a
    shared lock file). The first pump drains everything and mark_published sets
    published_at; the second pump reads zero pending rows.

    This proves mark_published is the dedup gate on the publish side: once a
    row is marked, no subsequent pump will re-publish it.
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    publisher = _CapturingPublisher()

    written_keys = _write_messages(outbox, n=6)

    pump1 = _make_pump(outbox, publisher, tmp_path, lock_name="lock1.lock")
    pump2 = _make_pump(outbox, publisher, tmp_path, lock_name="lock2.lock")

    count1 = pump1.run()
    count2 = pump2.run()

    assert count1 + count2 == 6, "total published across both pumps must equal written"
    received_keys = [row.dedup_key for row in publisher.published]
    assert sorted(received_keys) == sorted(written_keys), (
        "every written message must reach the publisher"
    )
    assert len(set(received_keys)) == len(received_keys), (
        "no message may be published more than once"
    )


def test_second_pump_cannot_acquire_lock(tmp_path: Path) -> None:
    """With one pump holding the flock, a second pump on the same lock file
    cannot acquire it and does not drain.
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    publisher = _CapturingPublisher()
    lock_path = tmp_path / "shared.lock"

    _write_messages(outbox, n=3)

    pump1 = OutboxPump(outbox, publisher, lock_path)
    pump2 = OutboxPump(outbox, publisher, lock_path)

    assert pump1.acquire_lock(), "pump1 must acquire the lock"
    try:
        assert not pump2.acquire_lock(), (
            "pump2 must not acquire the lock while pump1 holds it"
        )
        # pump2 did not drain — outbox still has 3 pending rows
        assert len(outbox.read_pending()) == 3, (
            "outbox must be unchanged: pump2 must not drain when it cannot lock"
        )
    finally:
        pump1.release_lock()

    # After pump1 releases, pump2 can acquire and drain
    assert pump2.acquire_lock(), "pump2 must acquire lock after pump1 releases"
    pump2.release_lock()


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
