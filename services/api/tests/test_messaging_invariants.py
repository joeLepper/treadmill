"""Ported invariant tests for the messaging library (ADR-0093, plan 6791c1cd).

Provenance: ramjac-events invariant test suite, ported to Treadmill's messaging
package. Each broken variant is shown RED (fails against the foil) then GREEN
(passes against the correct code), proving the test is non-vacuous and would
have caught the original bug.

Two load-bearing invariants (donna-identified, 2026-08-12):

  INVARIANT-1 (silent-crash / mark-then-process):
    The dedupKey MUST be recorded AFTER the handler succeeds, not before.
    Recording before (mark-then-process) permanently marks a message that was
    never processed — a mid-handler crash silently loses it.

  INVARIANT-2 (unscoped-dedup-collision):
    Dedup MUST be scoped to (dedupKey, consumer_id), not dedupKey alone.
    A key committed by subscriber-A must NOT block subscriber-B from processing
    the same event (ADR-0093 Decision, per-subscriber delivery rule;
    provenance: ramjac ADR-0014 §3 — dedup on event_id × subscriber_id).
"""

from __future__ import annotations

import uuid
from datetime import timedelta, datetime, timezone
from typing import Any

import pytest

from treadmill_api.messaging.consumer import MessageConsumer
from treadmill_api.messaging.dedup import ClaimResult, DedupBackend


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _msg(dedup_key: str | None = None) -> dict[str, Any]:
    return {
        "dedupKey": dedup_key or str(uuid.uuid4()),
        "ordering_key": "invariant-test",
        "event_type": "test.event",
        "payload": {},
    }


class _CountingConsumer(MessageConsumer):
    """Real consumer (claim→handle→commit) used for the GREEN proofs."""

    def __init__(self, dedup: DedupBackend, crash_window: timedelta) -> None:
        super().__init__(dedup, crash_window)
        self.calls: list[dict] = []

    def handle(self, message: dict) -> None:
        self.calls.append(message)


class _FailOnFirstConsumer(MessageConsumer):
    """Real consumer whose handle() raises on the first call, succeeds after."""

    def __init__(self, dedup: DedupBackend, crash_window: timedelta) -> None:
        super().__init__(dedup, crash_window)
        self.calls: list[dict] = []
        self._failed = False

    def handle(self, message: dict) -> None:
        if not self._failed:
            self._failed = True
            raise RuntimeError("simulated mid-handler crash")
        self.calls.append(message)


# ═══════════════════════════════════════════════════════════════════════════════
# INVARIANT-1 — silent-crash / mark-then-process
# Provenance: ramjac-events test_silent_crash_foil, ported 2026-08-13 (f6db308b)
# ═══════════════════════════════════════════════════════════════════════════════


class _MarkThenProcessConsumer:
    """BROKEN variant — records the dedupKey BEFORE the handler runs.

    This is the mark-then-process anti-pattern ADR-0093 Decision §3 forbids.
    A mid-handler crash leaves the key committed, so the message is never
    redelivered. The handler's work is lost silently.

    Provenance: foil ported from ramjac-events invariant suite (f6db308b).
    """

    def __init__(self, dedup: DedupBackend, crash_window: timedelta) -> None:
        self._dedup = dedup
        self._crash_window = crash_window
        self.calls: list[dict] = []

    def deliver(self, message: dict) -> None:
        dedup_key = message["dedupKey"]
        now = datetime.now(timezone.utc)
        result = self._dedup.claim(dedup_key, now, self._crash_window)
        if result != ClaimResult.GRANTED:
            return
        # BUG: commit BEFORE the handler runs.
        self._dedup.commit(dedup_key)  # mark-then-process: wrong order
        self._handle(message)

    def _handle(self, message: dict) -> None:
        self.calls.append(message)


class _MarkThenProcessCrashingConsumer(_MarkThenProcessConsumer):
    """BROKEN variant that also crashes on first delivery (simulates the loss)."""

    def __init__(self, dedup: DedupBackend, crash_window: timedelta) -> None:
        super().__init__(dedup, crash_window)
        self._failed = False

    def _handle(self, message: dict) -> None:
        if not self._failed:
            self._failed = True
            raise RuntimeError("simulated mid-handler crash after mark-then-process")
        super()._handle(message)


def test_red_mark_then_process_silently_loses_message(tmp_path) -> None:
    """RED (INVARIANT-1): mark-then-process commits the key before the handler
    runs. A mid-handler crash leaves the key committed; redelivery drops the
    message as DUPLICATE. The handler never runs successfully — the message
    is silently lost.

    The broken _MarkThenProcessCrashingConsumer demonstrates the bug.
    A correctly-failing assertion here proves the invariant test is non-vacuous.

    Provenance: ramjac-events test_silent_crash_foil (ported 2026-08-13, f6db308b).
    """
    db = tmp_path / "dedup-red.db"
    dedup = DedupBackend(db, consumer_id="consumer-red")
    consumer = _MarkThenProcessCrashingConsumer(
        dedup, crash_window=timedelta(seconds=0)
    )
    msg = _msg()

    # First delivery: key committed before handler; handler raises.
    with pytest.raises(RuntimeError, match="simulated mid-handler crash"):
        consumer.deliver(msg)

    # Second delivery: key is already committed → DUPLICATE → dropped.
    # The handler NEVER runs successfully. The message is silently lost.
    consumer.deliver(msg)

    # RED assertion: the handler ran 0 times. The broken variant loses the message.
    assert len(consumer.calls) == 0, (
        "RED foil: mark-then-process commits before handling; "
        "a crash silently loses the message (handler ran 0 times). "
        "If this assertion fails, the foil is no longer broken — check the test."
    )


def test_green_process_then_commit_redelivers_after_crash(tmp_path) -> None:
    """GREEN (INVARIANT-1): the correct claim→handle→commit cycle redelivers the
    message after a mid-handler crash. Handler runs exactly once (on redelivery).

    crash_window=0 lets the expired claim be immediately re-granted on the
    second delivery without wall-clock delay.

    Provenance: ramjac-events invariant test (ported 2026-08-13, f6db308b).
    """
    db = tmp_path / "dedup-green.db"
    dedup = DedupBackend(db, consumer_id="consumer-green")
    consumer = _FailOnFirstConsumer(dedup, crash_window=timedelta(seconds=0))
    msg = _msg()

    # First delivery: claim granted, handler raises, NO commit.
    with pytest.raises(RuntimeError, match="simulated mid-handler crash"):
        consumer.deliver(msg)

    # Second delivery: claim expired → re-granted, handler succeeds, committed.
    consumer.deliver(msg)

    # Third delivery: key committed → DUPLICATE → dropped.
    consumer.deliver(msg)

    assert len(consumer.calls) == 1, (
        "GREEN: correct consumer redelivers after crash; handler runs exactly once."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# INVARIANT-2 — unscoped-dedup-collision
# Provenance: ramjac-events test_unscoped_dedup_collision, ported 2026-08-13
# ═══════════════════════════════════════════════════════════════════════════════


class _UnscopedDedupConsumer:
    """BROKEN variant — deduplicates on dedupKey alone, ignoring consumer_id.

    Uses a single shared consumer_id for ALL consumers. A key committed by
    subscriber-A permanently blocks subscriber-B from processing the same event.

    This violates ADR-0093 Decision (per-subscriber delivery) and ramjac
    ADR-0014 §3 (dedup on event_id × subscriber_id).

    Provenance: foil ported from ramjac-events invariant suite (f6db308b).
    """

    SHARED_CONSUMER_ID = "global-dedup"  # bug: single ID shared by everyone

    def __init__(self, db_path, label: str) -> None:
        self._label = label
        # All instances share the SAME consumer_id → dedup is global, not per-consumer.
        self._dedup = DedupBackend(db_path, consumer_id=self.SHARED_CONSUMER_ID)
        self.calls: list[dict] = []

    def deliver(self, message: dict) -> None:
        dedup_key = message["dedupKey"]
        now = datetime.now(timezone.utc)
        result = self._dedup.claim(dedup_key, now, timedelta(minutes=5))
        if result != ClaimResult.GRANTED:
            return
        self.calls.append(message)
        self._dedup.commit(dedup_key)


def test_red_unscoped_dedup_drops_second_subscriber(tmp_path) -> None:
    """RED (INVARIANT-2): unscoped dedup (shared consumer_id) commits the key
    for subscriber-A, then blocks subscriber-B from processing the same event.
    Subscriber-B's delivery is dropped — a per-subscriber message is silently lost.

    The broken _UnscopedDedupConsumer demonstrates the collision.

    Provenance: ramjac-events test_unscoped_dedup_collision foil (ported 2026-08-13,
    f6db308b).
    """
    db = tmp_path / "dedup-unscoped.db"
    key = str(uuid.uuid4())
    msg = _msg(key)

    consumer_a = _UnscopedDedupConsumer(db, label="subscriber-a")
    consumer_b = _UnscopedDedupConsumer(db, label="subscriber-b")

    consumer_a.deliver(msg)  # subscriber-A commits with shared consumer_id
    consumer_b.deliver(msg)  # subscriber-B: sees DUPLICATE → dropped

    # RED assertion: subscriber-B processed 0 messages.
    assert len(consumer_b.calls) == 0, (
        "RED foil: unscoped dedup (shared consumer_id) blocks subscriber-B "
        "after subscriber-A commits. If this assertion fails, the foil is "
        "no longer broken — check the test."
    )
    # (subscriber-A did process correctly in the broken variant.)
    assert len(consumer_a.calls) == 1


def test_green_scoped_dedup_each_subscriber_processes_once(tmp_path) -> None:
    """GREEN (INVARIANT-2): correct dedup is scoped to (dedupKey, consumer_id).
    Both subscribers process the same event exactly once, independently.

    Provenance: ramjac-events invariant test (ported 2026-08-13, f6db308b).
    """
    db = tmp_path / "dedup-scoped.db"
    key = str(uuid.uuid4())
    msg = _msg(key)

    dedup_a = DedupBackend(db, consumer_id="subscriber-a")
    dedup_b = DedupBackend(db, consumer_id="subscriber-b")

    consumer_a = _CountingConsumer(dedup_a, crash_window=timedelta(minutes=5))
    consumer_b = _CountingConsumer(dedup_b, crash_window=timedelta(minutes=5))

    consumer_a.deliver(msg)
    consumer_b.deliver(msg)

    assert len(consumer_a.calls) == 1, "subscriber-a must process exactly once"
    assert len(consumer_b.calls) == 1, "subscriber-b must process exactly once (independent)"


# ═══════════════════════════════════════════════════════════════════════════════
# Claim / commit / expiry unit tests
# Provenance: ramjac-events DedupBackend unit tests (ported 2026-08-13, f6db308b)
# ═══════════════════════════════════════════════════════════════════════════════


def test_claim_grants_fresh_key(tmp_path) -> None:
    """A key not previously seen is GRANTED on first claim.

    Provenance: ramjac-events test_claim_fresh_key (ported 2026-08-13, f6db308b).
    """
    db = tmp_path / "dedup.db"
    dedup = DedupBackend(db, consumer_id="c1")
    now = datetime.now(timezone.utc)
    result = dedup.claim("key-a", now, timedelta(minutes=5))
    assert result == ClaimResult.GRANTED


def test_claim_before_commit_is_claimed_elsewhere(tmp_path) -> None:
    """A key claimed but not yet committed is CLAIMED_ELSEWHERE for concurrent
    claimants within the crash window.

    Provenance: ramjac-events test_claim_in_window (ported 2026-08-13, f6db308b).
    """
    db = tmp_path / "dedup.db"
    dedup = DedupBackend(db, consumer_id="c1")
    now = datetime.now(timezone.utc)

    r1 = dedup.claim("key-b", now, timedelta(minutes=5))
    assert r1 == ClaimResult.GRANTED

    # Second claim within the crash window → CLAIMED_ELSEWHERE.
    r2 = dedup.claim("key-b", now, timedelta(minutes=5))
    assert r2 == ClaimResult.CLAIMED_ELSEWHERE


def test_claim_after_commit_is_duplicate(tmp_path) -> None:
    """A committed key returns DUPLICATE on any subsequent claim.

    Provenance: ramjac-events test_claim_after_commit (ported 2026-08-13, f6db308b).
    """
    db = tmp_path / "dedup.db"
    dedup = DedupBackend(db, consumer_id="c1")
    now = datetime.now(timezone.utc)

    dedup.claim("key-c", now, timedelta(minutes=5))
    dedup.commit("key-c")

    result = dedup.claim("key-c", now, timedelta(minutes=5))
    assert result == ClaimResult.DUPLICATE


def test_expired_claim_is_reclaimable(tmp_path) -> None:
    """A claim whose crash window has passed (crash_window=0) is re-claimable.
    This is the recovery path for a dead handler.

    Provenance: ramjac-events test_expired_claim_reclaimable (ported 2026-08-13,
    f6db308b).
    """
    db = tmp_path / "dedup.db"
    dedup = DedupBackend(db, consumer_id="c1")
    now = datetime.now(timezone.utc)

    r1 = dedup.claim("key-d", now, timedelta(seconds=0))
    assert r1 == ClaimResult.GRANTED

    # expires_at == claimed_at (0s window) → already expired
    r2 = dedup.claim("key-d", now, timedelta(seconds=0))
    assert r2 == ClaimResult.GRANTED, (
        "expired claim must be re-claimable; this is the crash-recovery path"
    )


def test_commit_is_terminal_across_redeliveries(tmp_path) -> None:
    """After commit, any number of redeliveries return DUPLICATE.
    commit() is a terminal transition.

    Provenance: ramjac-events test_commit_terminal (ported 2026-08-13, f6db308b).
    """
    db = tmp_path / "dedup.db"
    dedup = DedupBackend(db, consumer_id="c1")
    now = datetime.now(timezone.utc)

    dedup.claim("key-e", now, timedelta(minutes=5))
    dedup.commit("key-e")

    for _ in range(3):
        result = dedup.claim("key-e", now, timedelta(minutes=5))
        assert result == ClaimResult.DUPLICATE, "committed key must remain DUPLICATE"


def test_different_consumer_ids_are_independent(tmp_path) -> None:
    """Two DedupBackends on the same db with different consumer_ids are
    fully independent — a claim or commit in one does not affect the other.

    Provenance: ramjac-events test_consumer_id_isolation (ported 2026-08-13,
    f6db308b).
    """
    db = tmp_path / "dedup.db"
    dedup_x = DedupBackend(db, consumer_id="x")
    dedup_y = DedupBackend(db, consumer_id="y")
    now = datetime.now(timezone.utc)

    dedup_x.claim("shared-key", now, timedelta(minutes=5))
    dedup_x.commit("shared-key")

    # y is independent — should still GRANT on fresh claim.
    result = dedup_y.claim("shared-key", now, timedelta(minutes=5))
    assert result == ClaimResult.GRANTED, (
        "consumer_id=y must be independent of consumer_id=x; "
        "a commit in x must not affect y"
    )
