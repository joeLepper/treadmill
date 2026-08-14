"""Tests for treadmill_api/messaging/host_consumer.py (ADR-0093).

Four invariants, each proven with red-then-green where required:

HOST-SCOPED      — consumer delivers only to labels bound to its own host.
EFFECTIVELY-ONCE — claim→handle→commit prevents double delivery per dedupKey.
PER-RECIPIENT ORDER — messages to one label arrive in ascending offset order.
ISOLATION FOIL   — red: naive code relays cross-host labels; green: real consumer does not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from treadmill_api.messaging.dedup import DedupBackend
from treadmill_api.messaging.host_consumer import HostConsumer


# ── Fake stores ───────────────────────────────────────────────────────────────


class _StubHostStore:
    """Returns a pre-configured label list for each host."""

    def __init__(self, label_map: dict[str, list[str]]) -> None:
        self._map = label_map

    async def labels_on_host(self, session: Any, host: str) -> list[str]:
        return list(self._map.get(host, []))


@dataclass
class _FakeRow:
    """Fake EventLog row with the fields HostConsumer reads."""

    offset: int
    dedup_key: str
    ordering_key: str
    event_type: str
    payload: dict = field(default_factory=dict)


class _StubLogStore:
    """Returns pre-configured rows filtered to matching labels and offset."""

    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    async def read_for_labels(
        self,
        session: Any,
        labels: list[str],
        after_offset: int = 0,
        limit: int = 100,
    ) -> list[_FakeRow]:
        return [
            r
            for r in self._rows
            if r.ordering_key in labels and r.offset > after_offset
        ]


def _row(offset: int, label: str, dedup_key: str | None = None) -> _FakeRow:
    return _FakeRow(
        offset=offset,
        dedup_key=dedup_key or str(uuid.uuid4()),
        ordering_key=label,
        event_type="test.event",
        payload={"seq": offset},
    )


def _consumer(
    host: str,
    host_store: _StubHostStore,
    log_store: _StubLogStore,
    tmp_path: Path,
    spy: list[str],
    crash_window: timedelta = timedelta(minutes=5),
) -> HostConsumer:
    dedup = DedupBackend(tmp_path / "dedup.db", consumer_id=host)
    return HostConsumer(
        host,
        host_store,
        log_store,
        dedup,
        relay_fn=lambda label, _msg: spy.append(label),
        crash_window=crash_window,
    )


# ── HOST-SCOPED ───────────────────────────────────────────────────────────────


async def test_host_scoped_delivers_only_own_labels(tmp_path: Path) -> None:
    """Consumer for host-a delivers only host-a-bound labels, never host-b labels."""
    relayed: list[str] = []
    host_store = _StubHostStore({"host-a": ["alice"], "host-b": ["bob"]})
    log_store = _StubLogStore([_row(1, "alice"), _row(2, "bob")])
    c = _consumer("host-a", host_store, log_store, tmp_path, relayed)

    await c.run_once(session=None)

    assert relayed == ["alice"], f"host-a must relay only 'alice'; got {relayed}"


async def test_host_scoped_empty_bindings_delivers_nothing(tmp_path: Path) -> None:
    """Consumer with no bound labels delivers nothing."""
    relayed: list[str] = []
    host_store = _StubHostStore({})
    log_store = _StubLogStore([_row(1, "alice")])
    c = _consumer("host-a", host_store, log_store, tmp_path, relayed)

    count = await c.run_once(session=None)

    assert count == 0
    assert relayed == []


async def test_host_scoped_multiple_own_labels_all_delivered(tmp_path: Path) -> None:
    """Consumer delivers to all labels bound to its host."""
    relayed: list[str] = []
    host_store = _StubHostStore({"host-a": ["alice", "carol"]})
    log_store = _StubLogStore([_row(1, "alice"), _row(2, "carol"), _row(3, "bob")])
    c = _consumer("host-a", host_store, log_store, tmp_path, relayed)

    await c.run_once(session=None)

    assert set(relayed) == {"alice", "carol"}, f"expected alice+carol; got {relayed}"
    assert "bob" not in relayed


# ── EFFECTIVELY-ONCE ─────────────────────────────────────────────────────────


async def test_effectively_once_same_key_handled_once(tmp_path: Path) -> None:
    """Same dedupKey delivered twice — handler runs exactly once."""
    relayed: list[str] = []
    key = str(uuid.uuid4())
    host_store = _StubHostStore({"host-a": ["alice"]})
    log_store = _StubLogStore([_row(1, "alice", dedup_key=key)])
    dedup = DedupBackend(tmp_path / "dedup.db", consumer_id="host-a")
    c = HostConsumer(
        "host-a",
        host_store,
        log_store,
        dedup,
        relay_fn=lambda label, _: relayed.append(label),
        crash_window=timedelta(minutes=5),
    )

    # First delivery — cursor at 0, row at offset 1 → delivers and commits
    await c.run_once(session=None)
    # Reset cursor to simulate re-delivery of the same row
    c._cursor = 0
    await c.run_once(session=None)

    assert len(relayed) == 1, f"handler must run once; ran {len(relayed)} times"


async def test_effectively_once_distinct_keys_both_handled(tmp_path: Path) -> None:
    """Two distinct messages → handler called twice (one per key)."""
    relayed: list[str] = []
    host_store = _StubHostStore({"host-a": ["alice"]})
    log_store = _StubLogStore([_row(1, "alice"), _row(2, "alice")])
    c = _consumer("host-a", host_store, log_store, tmp_path, relayed)

    await c.run_once(session=None)

    assert len(relayed) == 2, f"two distinct keys must each be handled; got {relayed}"


# ── PER-RECIPIENT ORDER ───────────────────────────────────────────────────────


async def test_per_recipient_order_ascending_offsets(tmp_path: Path) -> None:
    """Three messages to the same label arrive in ascending offset order."""
    sequence: list[int] = []

    host_store = _StubHostStore({"host-a": ["alice"]})
    log_store = _StubLogStore([_row(1, "alice"), _row(2, "alice"), _row(3, "alice")])
    dedup = DedupBackend(tmp_path / "dedup.db", consumer_id="host-a")
    c = HostConsumer(
        "host-a",
        host_store,
        log_store,
        dedup,
        relay_fn=lambda _label, msg: sequence.append(msg["payload"]["seq"]),
        crash_window=timedelta(minutes=5),
    )

    await c.run_once(session=None)

    assert sequence == [1, 2, 3], f"expected [1,2,3] in offset order; got {sequence}"


async def test_per_recipient_order_cursor_advances_to_highest_offset(tmp_path: Path) -> None:
    """After run_once, the cursor equals the highest offset delivered."""
    relayed: list[str] = []
    host_store = _StubHostStore({"host-a": ["alice"]})
    log_store = _StubLogStore([_row(5, "alice"), _row(10, "alice")])
    c = _consumer("host-a", host_store, log_store, tmp_path, relayed)

    await c.run_once(session=None)

    assert c._cursor == 10, f"cursor must advance to 10; got {c._cursor}"


async def test_per_recipient_order_cursor_gates_subsequent_poll(tmp_path: Path) -> None:
    """Second poll with advanced cursor does not re-deliver already-seen rows."""
    relayed: list[str] = []
    host_store = _StubHostStore({"host-a": ["alice"]})
    log_store = _StubLogStore([_row(1, "alice"), _row(2, "alice")])
    c = _consumer("host-a", host_store, log_store, tmp_path, relayed)

    await c.run_once(session=None)   # delivers offsets 1 and 2; cursor→2
    relayed.clear()
    await c.run_once(session=None)   # no rows after offset 2 → nothing delivered

    assert relayed == [], f"second poll must not re-deliver past rows; got {relayed}"


# ── ISOLATION FOIL (red-then-green) ──────────────────────────────────────────


def _naive_consume(rows: list[_FakeRow], relay_fn: Any) -> list[str]:
    """Naive consumer: relays ALL log rows with no host-scope filter.

    RED: violates the isolation invariant — touches labels not bound to this host.
    """
    relayed: list[str] = []
    for row in rows:
        relay_fn(row.ordering_key, {"dedupKey": row.dedup_key, "ordering_key": row.ordering_key})
        relayed.append(row.ordering_key)
    return relayed


def test_isolation_foil_red_naive_relays_cross_host_label() -> None:
    """RED: naive consumer relays 'bob' (host-b label) when run on host-a.

    Proves the isolation invariant is non-vacuous — a naive implementation
    violates it. The green counterpart shows the real consumer does not.
    """
    relayed: list[str] = []
    rows = [_row(1, "alice"), _row(2, "bob")]  # alice=host-a, bob=host-b

    _naive_consume(rows, relay_fn=lambda label, _: relayed.append(label))

    # RED: naive code DOES relay the cross-host label — this is the violation
    assert "bob" in relayed, (
        "red: _naive_consume must relay 'bob' to prove the foil is non-vacuous"
    )


async def test_isolation_foil_green_real_consumer_skips_cross_host(tmp_path: Path) -> None:
    """GREEN: HostConsumer never relays labels not bound to its host.

    host-a has only 'alice'. The log has events for 'alice' AND 'bob'
    (host-b's label). The real HostConsumer must relay only 'alice'.
    """
    relayed: list[str] = []
    host_store = _StubHostStore({"host-a": ["alice"]})
    log_store = _StubLogStore([_row(1, "alice"), _row(2, "bob")])
    c = _consumer("host-a", host_store, log_store, tmp_path, relayed)

    await c.run_once(session=None)

    assert "bob" not in relayed, (
        f"green: HostConsumer must NOT relay 'bob' (cross-host label); got {relayed}"
    )
    assert "alice" in relayed, "green: HostConsumer must relay 'alice' (own label)"
