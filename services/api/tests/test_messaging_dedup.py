"""Dedup consumer tests.

Covers the two key dedup properties:
- same dedupKey through one consumer runs the handler exactly once
- same dedupKey through two consumers with different consumer_ids each
  runs once (dedup is scoped per consumer, not key-global)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from treadmill_api.messaging.consumer import MessageConsumer
from treadmill_api.messaging.dedup import DedupBackend


def _make_message(dedup_key: str | None = None) -> dict:
    return {
        "dedupKey": dedup_key or str(uuid.uuid4()),
        "ordering_key": "test-consumer",
        "event_type": "test.event",
        "payload": {},
    }


class _CountingConsumer(MessageConsumer):
    def __init__(self, dedup: DedupBackend, crash_window: timedelta) -> None:
        super().__init__(dedup, crash_window)
        self.calls: list[dict] = []

    def handle(self, message: dict) -> None:
        self.calls.append(message)


def test_same_dedupkey_twice_runs_handler_once(tmp_path):
    """Publishing the same dedupKey twice — second delivery is dropped."""
    db = tmp_path / "dedup.db"
    dedup = DedupBackend(db, consumer_id="consumer-a")
    consumer = _CountingConsumer(dedup, crash_window=timedelta(minutes=5))

    msg = _make_message()
    consumer.deliver(msg)
    consumer.deliver(msg)

    assert len(consumer.calls) == 1, "handler must run exactly once for duplicate key"


def test_unscoped_dedup_collision(tmp_path):
    """Two consumers with the same dedupKey but different consumer_ids
    each process the message once.

    Dedup is scoped to (dedup_key, consumer_id). A key committed by
    consumer-a does not block consumer-b.
    """
    db = tmp_path / "dedup.db"
    key = str(uuid.uuid4())

    dedup_a = DedupBackend(db, consumer_id="consumer-a")
    dedup_b = DedupBackend(db, consumer_id="consumer-b")

    consumer_a = _CountingConsumer(dedup_a, crash_window=timedelta(minutes=5))
    consumer_b = _CountingConsumer(dedup_b, crash_window=timedelta(minutes=5))

    msg = _make_message(key)
    consumer_a.deliver(msg)
    consumer_b.deliver(msg)

    assert len(consumer_a.calls) == 1, "consumer-a must process the message once"
    assert len(consumer_b.calls) == 1, "consumer-b must process the message once"
