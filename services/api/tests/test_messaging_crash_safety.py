"""Crash-safety tests — the gate.

These tests verify the core property of ADR-0093: a message that is
claimed but not committed (because the handler raised or the process died)
is redelivered once the crash window expires, and processed exactly once
on redelivery.

Property 3 from ADR-0093:
  CLAIM the dedupKey → RUN the handler → COMMIT the key.
  Never mark-then-process.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from treadmill_api.messaging.consumer import MessageConsumer
from treadmill_api.messaging.dedup import ClaimResult, DedupBackend


def _make_message(dedup_key: str | None = None) -> dict:
    return {
        "dedupKey": dedup_key or str(uuid.uuid4()),
        "ordering_key": "test-consumer",
        "event_type": "test.event",
        "payload": {},
    }


class _FailOnceConsumer(MessageConsumer):
    """Handler that raises on the first call, succeeds on subsequent calls."""

    def __init__(self, dedup: DedupBackend, crash_window: timedelta) -> None:
        super().__init__(dedup, crash_window)
        self.calls: list[dict] = []
        self._first = True

    def handle(self, message: dict) -> None:
        if self._first:
            self._first = False
            raise RuntimeError("simulated handler crash")
        self.calls.append(message)


class _SucceedingConsumer(MessageConsumer):
    def __init__(self, dedup: DedupBackend, crash_window: timedelta) -> None:
        super().__init__(dedup, crash_window)
        self.calls: list[dict] = []

    def handle(self, message: dict) -> None:
        self.calls.append(message)


def test_crash_mid_handler_redelivers_and_processes_once(tmp_path):
    """THE GATE.

    Sequence:
    1. Deliver message. Claim granted. Handler raises. Claim NOT committed.
    2. Deliver same message. Crash window (0s) is expired. Claim re-granted.
       Handler succeeds. Committed.
    3. Deliver same message. Key committed. Dropped.

    The handler must run exactly once total (on the second delivery).
    """
    db = tmp_path / "dedup.db"
    # crash_window=0 makes any claim expire immediately after creation so
    # the second delivery can re-claim without wall-clock delay.
    dedup = DedupBackend(db, consumer_id="consumer-crash-test")
    consumer = _FailOnceConsumer(dedup, crash_window=timedelta(seconds=0))

    msg = _make_message()

    # First delivery: handler raises → no commit.
    with pytest.raises(RuntimeError, match="simulated handler crash"):
        consumer.deliver(msg)

    # Second delivery: claim is expired (crash_window=0) → re-granted,
    # handler succeeds, committed.
    consumer.deliver(msg)

    # Third delivery: key is committed → dropped.
    consumer.deliver(msg)

    assert len(consumer.calls) == 1, (
        "handler must run exactly once: first delivery raised (no commit), "
        "second delivery re-claimed and committed, third delivery dropped"
    )


def test_success_then_crash_before_commit_reruns_safely(tmp_path):
    """Idempotency contract: handler succeeds, process dies before commit,
    message redelivers, handler runs again, net effect is correct.

    Simulates 'process dies before commit' by manually calling claim +
    handle without commit (bypassing the consumer's commit step), then
    redelivering through the consumer once the crash window expires.

    An idempotent handler yields one net effect even when it runs twice.
    """
    db = tmp_path / "dedup.db"
    key = str(uuid.uuid4())
    msg = _make_message(key)

    dedup = DedupBackend(db, consumer_id="consumer-idem-test")
    consumer = _SucceedingConsumer(dedup, crash_window=timedelta(seconds=0))

    # Step 1: simulate "claim granted, handler runs, process dies before commit".
    # We call claim() and handle() directly, skipping commit().
    now = datetime.now(timezone.utc)
    result = dedup.claim(key, now, crash_window=timedelta(seconds=0))
    assert result == ClaimResult.GRANTED
    consumer.calls.append(msg)  # simulate the handler running

    # Step 2: redeliver via consumer. crash_window=0 so claim is expired.
    # Consumer re-claims, runs handle(), commits.
    consumer.deliver(msg)

    # Step 3: deliver again. Key is committed. Dropped.
    consumer.deliver(msg)

    # The handler ran twice total (step 1 manual + step 2 via deliver).
    # An idempotent handler produces one net effect despite two executions.
    assert len(consumer.calls) == 2, (
        "handler ran twice (once before crash, once on redelivery); "
        "idempotent handlers must tolerate this"
    )
    # The key is now committed — no further processing.
    now2 = datetime.now(timezone.utc)
    result2 = dedup.claim(key, now2, crash_window=timedelta(minutes=5))
    assert result2 == ClaimResult.DUPLICATE
