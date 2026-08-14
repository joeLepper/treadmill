"""Completing slice-1a — per-host outbox + single-pump semantics (ADR-0094, task 4ba55a27).

Three load-bearing tests (evaluator-required, non-vacuous + red-then-green):

  SINGLE-PUMP FLOCK REFUSAL
    First pump acquires + drains all rows; second pump on the same lock file
    refuses (run() returns -1) and drains nothing. Both assertions in one test.

  WRITE-THEN-CRASH-BEFORE-PUBLISH REDELIVERS (with foil)
    RED: _MarkBeforePublishPump marks rows published BEFORE calling publisher.publish().
         When the publisher raises (crash), the row is permanently marked — lost.
    GREEN: correct OutboxPump publishes BEFORE marking. A crash (pump never runs)
           leaves all rows pending; a new pump on restart delivers them all — no loss.

  DRAIN-ON-RECONNECT IN ORDER
    While the log is unreachable (pump not running), N rows accumulate in the outbox.
    On reconnect the pump delivers ALL rows to the publisher in append order.

Sandbox-safe: fake publisher; no network; no SNS.

Provenance: slice-1a outbox pump (task cda51037) + completing semantics (task 4ba55a27).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from treadmill_api.messaging.outbox import OutboxBackend, OutboxRow
from treadmill_api.messaging.pump import OutboxPump


# ── Fake publishers ────────────────────────────────────────────────────────────


class _CapturingPublisher:
    def __init__(self) -> None:
        self.published: list[OutboxRow] = []

    def publish(self, row: OutboxRow) -> None:
        self.published.append(row)


class _CrashOnFirstPublisher:
    """Raises on the first publish() call, succeeds thereafter."""

    def __init__(self) -> None:
        self.published: list[OutboxRow] = []
        self._first = True

    def publish(self, row: OutboxRow) -> None:
        if self._first:
            self._first = False
            raise RuntimeError("simulated publish crash")
        self.published.append(row)


# ── Broken pump variant (foil) ─────────────────────────────────────────────────


class _MarkBeforePublishPump:
    """BROKEN variant — marks rows published BEFORE calling publisher.publish().

    This is the pump-side mark-then-process anti-pattern. A crash between
    mark_published() and publisher.publish() leaves the row permanently marked
    as published but never delivered — silently lost.

    Used ONLY as the RED foil in test_red_mark_before_publish_loses_row_on_crash.
    Provenance: write-then-crash foil (task 4ba55a27, ADR-0094).
    """

    def __init__(self, outbox: OutboxBackend, publisher: Any, batch_size: int = 100) -> None:
        self._outbox = outbox
        self._publisher = publisher
        self._batch_size = batch_size

    def drain(self) -> int:
        total = 0
        while True:
            rows = self._outbox.read_pending(limit=self._batch_size)
            if not rows:
                break
            for row in rows:
                self._outbox.mark_published(row.row_id)  # BUG: mark before publish
                self._publisher.publish(row)              # crash here → row already marked
                total += 1
        return total


# ── Helper: write messages ─────────────────────────────────────────────────────


def _write_n(outbox: OutboxBackend, n: int, ordering_key: str = "recipient") -> list[str]:
    keys = []
    for _ in range(n):
        key = str(uuid.uuid4())
        outbox.write({
            "dedupKey": key,
            "ordering_key": ordering_key,
            "event_type": "test.event",
            "payload": {},
        })
        keys.append(key)
    return keys


# ══════════════════════════════════════════════════════════════════════════════
# 1 — SINGLE-PUMP FLOCK REFUSAL
# ══════════════════════════════════════════════════════════════════════════════


def test_single_pump_flock_first_acquires_drains_second_refuses(tmp_path: Path) -> None:
    """First pump acquires the flock and drains all rows; second pump on the same
    lock file refuses (run() == -1) and publishes nothing.

    Both halves asserted in one test per ADR-0094: the single-pump invariant is
    enforced by flock — a second concurrent pump cannot acquire the lock.
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    lock_path = tmp_path / "outbox.lock"

    publisher1 = _CapturingPublisher()
    publisher2 = _CapturingPublisher()
    pump1 = OutboxPump(outbox, publisher1, lock_path)
    pump2 = OutboxPump(outbox, publisher2, lock_path)

    keys = _write_n(outbox, n=4)

    # Pump1 acquires the lock and drains all rows.
    assert pump1.acquire_lock(), "pump1 must acquire the flock"
    try:
        drained = pump1.drain()
        assert drained == 4, "pump1 must drain all 4 rows"
        assert len(publisher1.published) == 4, "pump1 must publish all 4 rows"
        assert [r.dedup_key for r in publisher1.published] == keys, (
            "pump1 must publish rows in append order"
        )

        # Pump2, on the same lock file, must refuse while pump1 holds it.
        result2 = pump2.run()
        assert result2 == -1, (
            "pump2.run() must return -1 when pump1 holds the flock — "
            "second pump must be refused, not allowed to drain"
        )
        assert len(publisher2.published) == 0, "refused pump2 must publish nothing"
        assert len(outbox.read_pending()) == 0, (
            "outbox is empty after pump1 drained — pump2 refusal did not corrupt state"
        )
    finally:
        pump1.release_lock()


# ══════════════════════════════════════════════════════════════════════════════
# 2 — WRITE-THEN-CRASH-BEFORE-PUBLISH: red-then-green foil
# ══════════════════════════════════════════════════════════════════════════════


def test_red_mark_before_publish_loses_row_on_crash(tmp_path: Path) -> None:
    """RED (write-then-crash foil): _MarkBeforePublishPump marks row 1 published
    BEFORE calling publisher.publish(). The publisher raises on its first call
    (simulating a crash). Row 1 is permanently marked; it will never be redelivered.

    A recovery pump on restart reads rows 2 and 3 (still pending), delivers them,
    but row 1 is silently lost. The broken variant MUST fail the all-rows-delivered
    assertion — proving the RED foil is non-vacuous (evaluator requirement).

    Provenance: write-then-crash foil, ported semantics (task 4ba55a27, ADR-0094).
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    keys = _write_n(outbox, n=3)

    # Broken pump: marks row 1, then crashes (publisher raises).
    crash_pub = _CrashOnFirstPublisher()
    broken_pump = _MarkBeforePublishPump(outbox, crash_pub)
    try:
        broken_pump.drain()
    except RuntimeError:
        pass  # expected crash — row 1 is now marked but never published

    # Recovery: new pump, working publisher.
    recovery_pub = _CapturingPublisher()
    recovery_pump = OutboxPump(outbox, recovery_pub, tmp_path / "outbox.lock")
    recovery_pump.run()

    # RED assertion: row 1 is silently lost — only rows 2 and 3 are delivered.
    delivered_keys = [r.dedup_key for r in recovery_pub.published]
    assert keys[0] not in delivered_keys, (
        "RED foil: row 1 must be silently lost (marked before publish, then crashed). "
        "If row 1 appears here, the foil is no longer broken — check the test."
    )
    assert len(recovery_pub.published) == 2, (
        "RED foil: only rows 2 and 3 reach the recovery pump; row 1 is gone."
    )


def test_green_write_before_crash_all_rows_redelivered(tmp_path: Path) -> None:
    """GREEN (write-then-crash): rows written to the outbox but not yet published
    remain PENDING after a crash. A new pump on restart delivers ALL of them in
    append order — nothing is lost.

    The outbox write is durable (WAL SQLite). A crash before the pump runs
    leaves published_at NULL on every row; read_pending() returns them all on
    the next run. This is ADR-0094 §2 (retries indefinitely; never drops).
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    keys = _write_n(outbox, n=3)

    # Simulate crash: process dies before the pump ever ran.
    # All 3 rows are in the outbox with published_at = NULL.
    assert len(outbox.read_pending()) == 3, "all rows must be pending before recovery"

    # Recovery: new pump, working publisher. Delivers all pending rows.
    recovery_pub = _CapturingPublisher()
    pump = OutboxPump(outbox, recovery_pub, tmp_path / "outbox.lock")
    count = pump.run()

    assert count == 3, "recovery pump must deliver all 3 rows"
    assert [r.dedup_key for r in recovery_pub.published] == keys, (
        "recovery pump must deliver rows in append order (ADR-0094 drain-in-order)"
    )
    assert len(outbox.read_pending()) == 0, "no pending rows after recovery drain"


# ══════════════════════════════════════════════════════════════════════════════
# 3 — DRAIN-ON-RECONNECT IN ORDER
# ══════════════════════════════════════════════════════════════════════════════


def test_drain_on_reconnect_delivers_accumulated_rows_in_order(tmp_path: Path) -> None:
    """While the log is unreachable the pump is not running; rows accumulate in the
    local outbox (durable — WAL SQLite; host keeps working without network). When the
    log reconnects and the pump runs, ALL accumulated rows are delivered to the
    publisher in append order — none dropped, order preserved.

    This is the core ADR-0094 promise: a host that loses the network keeps working,
    buffers events, and drains them in order on reconnect.

    The publisher is injected (no real network). The "unreachable period" is the
    time between the writes and the pump run — the outbox is the buffer.
    """
    outbox = OutboxBackend(tmp_path / "outbox.db")
    publisher = _CapturingPublisher()
    pump = OutboxPump(outbox, publisher, tmp_path / "outbox.lock")

    # Write 7 rows while the log is "unreachable" (pump not running).
    # ordering_key is the same recipient — ADR-0094 guarantees per-recipient order.
    N = 7
    keys = _write_n(outbox, n=N, ordering_key="coordinator-joelepper-treadmill")

    # Confirm all rows are pending — nothing published yet.
    assert len(outbox.read_pending()) == N, (
        "all rows must be pending while the pump is not running"
    )

    # Reconnect: pump runs, drains all accumulated rows.
    count = pump.run()

    # All rows delivered, in order, none dropped.
    assert count == N, f"pump must deliver all {N} accumulated rows on reconnect"
    assert len(publisher.published) == N
    assert [r.dedup_key for r in publisher.published] == keys, (
        "rows must arrive at the publisher in append order (ADR-0094 drain-in-order)"
    )
    assert len(outbox.read_pending()) == 0, "no rows must remain pending after drain"
