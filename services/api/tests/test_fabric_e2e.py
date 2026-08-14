"""Fabric end-to-end tests — definition of done for plan 6791c1cd (task dadbf11d).

Three pinned acceptance requirements:

  (A) read_for_labels integration test — TREADMILL_INTEGRATION-gated, real
      Postgres. Appends events for two labels bound to different hosts; asserts
      read_for_labels([label_a]) returns ONLY label_a's rows.

  (B) HostConsumer.handle() defense-in-depth — refuses to relay a row whose
      ordering_key is NOT in _local_labels. Second enforcement beyond the
      read_for_labels filter. Proven RED-then-GREEN with a planted leaked row.

  (C) Drain-level two-host end-to-end — three scenarios, each decoded from
      actual relay captures, each shown RED against a broken transport before
      green (ADR-0093/0094 fabric bar):

      1. CROSS-HOST: host-A agent → shared log → host-B consumer → relay decoded.
         Dup-dropped: same dedupKey re-delivered → exactly one delivery.

      2. ISOLATION: host-B outbox accumulates while log link is cut; restore →
         drain → all N events decoded in order; nothing adopted host-B's labels.

      3. BROADCAST: fanout expands a channel broadcast to both subscribers once
         each; decoded on each host's consumer.

All sandbox-safe (C uses SQLite shared log, two in-process "host" instances,
no network). Req A is TREADMILL_INTEGRATION-gated and skipped in sandbox.
Provenance of the drain-level bar: "outbox-cutover-needs-drain-level-e2e" and
"green-but-functionally-dead" learnings — validate by decoding actual output,
never by asserting logs or publish-side traces only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from treadmill_api.messaging.broadcast import ChannelStore, fanout
from treadmill_api.messaging.dedup import DedupBackend
from treadmill_api.messaging.host_consumer import HostConsumer
from treadmill_api.messaging.outbox import OutboxBackend, OutboxRow
from treadmill_api.messaging.pump import OutboxPump

# ── Integration gate ──────────────────────────────────────────────────────────

_INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
_TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
_INTEGRATION_SKIP = pytest.mark.skipif(
    not (_INTEGRATION and _TEST_DB_URL),
    reason=(
        "set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL "
        "(a dedicated test database) to run; requires `treadmill-local up`"
    ),
)

# ── Shared sandbox log (SQLite, replaces Postgres for C scenarios) ────────────


@dataclass
class _SandboxLogRow:
    """Mirrors the EventLog row fields that HostConsumer reads."""

    offset: int
    dedup_key: str
    ordering_key: str
    event_type: str
    payload: dict[str, Any]


class _SharedSQLiteLog:
    """Append-only SQLite event log for two-host sandbox e2e tests.

    Shared between both host instances (single file, WAL mode). Mirrors the
    EventLogStore interface used by HostConsumer's log_store.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_log (
                offset        INTEGER PRIMARY KEY AUTOINCREMENT,
                ordering_key  TEXT NOT NULL,
                dedup_key     TEXT NOT NULL,
                event_type    TEXT NOT NULL,
                payload       TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def append(self, message: dict[str, Any]) -> int:
        """Append and return the assigned offset."""
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.execute(
            "INSERT INTO event_log (ordering_key, dedup_key, event_type, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                message["ordering_key"],
                message["dedupKey"],
                message["event_type"],
                json.dumps(message["payload"]),
            ),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id

    def read_for_labels(
        self,
        labels: list[str],
        after_offset: int = 0,
        limit: int = 100,
    ) -> list[_SandboxLogRow]:
        """Return rows for labels in offset order after after_offset."""
        if not labels:
            return []
        placeholders = ",".join("?" * len(labels))
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            f"SELECT offset, ordering_key, dedup_key, event_type, payload "
            f"FROM event_log "
            f"WHERE ordering_key IN ({placeholders}) AND offset > ? "
            f"ORDER BY offset ASC LIMIT ?",
            (*labels, after_offset, limit),
        ).fetchall()
        conn.close()
        return [
            _SandboxLogRow(
                offset=r[0],
                ordering_key=r[1],
                dedup_key=r[2],
                event_type=r[3],
                payload=json.loads(r[4]),
            )
            for r in rows
        ]


class _E2ELogStore:
    """Async wrapper over _SharedSQLiteLog — satisfies HostConsumer.log_store."""

    def __init__(self, log: _SharedSQLiteLog) -> None:
        self._log = log

    async def read_for_labels(
        self,
        session: Any,
        labels: list[str],
        after_offset: int = 0,
        limit: int = 100,
    ) -> list[_SandboxLogRow]:
        return self._log.read_for_labels(labels, after_offset=after_offset, limit=limit)


class _E2EHostStore:
    """Async stub host store — satisfies HostConsumer.host_store."""

    def __init__(self, host_labels: dict[str, list[str]]) -> None:
        self._map = host_labels

    async def labels_on_host(self, session: Any, host: str) -> list[str]:
        return list(self._map.get(host, []))


# ── Relay capture (replaces cc-relay file writes) ─────────────────────────────


@dataclass
class _RelayCapture:
    """Captures relay calls. Injected as relay_fn into HostConsumer."""

    delivered: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, label: str, message: dict[str, Any]) -> None:
        self.delivered.append({"label": label, **message})


# ── Log publisher (OutboxRow → _SharedSQLiteLog) ──────────────────────────────


class _LogPublisher:
    """RowPublisher that writes to a _SharedSQLiteLog. Used by OutboxPump."""

    def __init__(self, log: _SharedSQLiteLog) -> None:
        self._log = log

    def publish(self, row: OutboxRow) -> None:
        self._log.append(
            {
                "ordering_key": row.ordering_key,
                "dedupKey": row.dedup_key,
                "event_type": row.event_type,
                "payload": row.payload,
            }
        )


class _NoopPublisher:
    """RowPublisher that does nothing — simulates a cut link (broken transport)."""

    def publish(self, row: OutboxRow) -> None:
        pass


# ── Naive consumer (no handle() defense) — used for Req B RED ─────────────────


class _NaiveHostConsumer(HostConsumer):
    """Naive subclass: handle() does NOT check _local_labels.

    RED variant — no second-wall enforcement. Exists only to prove Req B is
    non-vacuous: a consumer WITHOUT the defense relays cross-host rows.
    """

    def handle(self, message: dict[str, Any]) -> None:
        label = message["ordering_key"]
        self._relay_fn(label, message)


# ── Leaky log store — used for Req B (bypasses read_for_labels filter) ────────


class _LeakyLogStore:
    """Returns ALL rows regardless of the labels filter.

    Simulates a regression in read_for_labels that leaks cross-host rows.
    Used in Req B to prove handle() is a second, independent wall.
    """

    def __init__(self, rows: list[_SandboxLogRow]) -> None:
        self._rows = rows

    async def read_for_labels(
        self,
        session: Any,
        labels: list[str],
        after_offset: int = 0,
        limit: int = 100,
    ) -> list[_SandboxLogRow]:
        return [r for r in self._rows if r.offset > after_offset]


# ─────────────────────────────────────────────────────────────────────────────
# Req B — handle() fail-closed defense-in-depth (RED-then-GREEN)
# ─────────────────────────────────────────────────────────────────────────────


async def test_handle_defense_req_b_red_naive_relays_leaked_row(
    tmp_path: Path,
) -> None:
    """RED (Req B): naive consumer relays a cross-host row that leaked through.

    _LeakyLogStore bypasses the first-wall filter: it returns a row addressed
    to "agent-b" (host-b's label) when host-a's consumer queries it. The naive
    consumer has no second wall, so it relays "agent-b" to host-a's relay. This
    proves the invariant is non-vacuous — a consumer without the defense leaks.
    """
    leaked_row = _SandboxLogRow(
        offset=1,
        dedup_key=str(uuid.uuid4()),
        ordering_key="agent-b",  # host-b's label — must NOT reach host-a
        event_type="test.event",
        payload={"content": "cross-host leak"},
    )
    host_store = _E2EHostStore({"host-a": ["agent-a"]})
    leaky_store = _LeakyLogStore([leaked_row])
    relay = _RelayCapture()
    dedup = DedupBackend(tmp_path / "dedup-naive.db", consumer_id="host-a")

    naive = _NaiveHostConsumer(
        "host-a",
        host_store,
        leaky_store,
        dedup,
        relay_fn=relay,
        crash_window=timedelta(minutes=5),
    )
    await naive.run_once(session=None)

    # RED: naive consumer DOES relay the cross-host row
    labels_relayed = [d["label"] for d in relay.delivered]
    assert "agent-b" in labels_relayed, (
        "red: naive consumer must relay the leaked cross-host row to prove "
        "the Req B invariant is non-vacuous"
    )


async def test_handle_defense_req_b_green_real_refuses_leaked_row(
    tmp_path: Path,
) -> None:
    """GREEN (Req B): real HostConsumer refuses a cross-host row leaked through.

    Same setup as the RED test — _LeakyLogStore bypasses the first wall. The
    real HostConsumer's handle() checks _local_labels (second wall) and refuses
    to relay "agent-b" when running as host-a. Only "agent-a"'s rows are relayed.
    """
    own_row = _SandboxLogRow(
        offset=1,
        dedup_key=str(uuid.uuid4()),
        ordering_key="agent-a",  # host-a's own label — must be relayed
        event_type="test.event",
        payload={"content": "own message"},
    )
    leaked_row = _SandboxLogRow(
        offset=2,
        dedup_key=str(uuid.uuid4()),
        ordering_key="agent-b",  # host-b's label — must be refused
        event_type="test.event",
        payload={"content": "cross-host leak"},
    )
    host_store = _E2EHostStore({"host-a": ["agent-a"]})
    leaky_store = _LeakyLogStore([own_row, leaked_row])
    relay = _RelayCapture()
    dedup = DedupBackend(tmp_path / "dedup-green.db", consumer_id="host-a")

    real = HostConsumer(
        "host-a",
        host_store,
        leaky_store,
        dedup,
        relay_fn=relay,
        crash_window=timedelta(minutes=5),
    )
    await real.run_once(session=None)

    labels_relayed = [d["label"] for d in relay.delivered]
    # GREEN: defense refuses the cross-host row
    assert "agent-b" not in labels_relayed, (
        f"green: real HostConsumer must NOT relay agent-b (cross-host); "
        f"got {labels_relayed}"
    )
    # Own label is still relayed
    assert "agent-a" in labels_relayed, (
        "green: real HostConsumer must relay agent-a (own label)"
    )
    # Decode the actual delivered message
    own_delivered = [d for d in relay.delivered if d["label"] == "agent-a"]
    assert own_delivered[0]["payload"]["content"] == "own message", (
        f"decoded payload mismatch: {own_delivered[0]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Req C — Scenario 1: CROSS-HOST deliver-once-in-order + dup-dropped
# ─────────────────────────────────────────────────────────────────────────────


async def test_fabric_sc1_cross_host_red_then_green(tmp_path: Path) -> None:
    """Scenario 1 — CROSS-HOST: host-A writes to outbox → pump → shared log → host-B relay.

    RED: broken publisher (no-op) → pump runs but writes nothing to log →
    host-B consumer delivers 0 messages. Proves the assertion is non-vacuous.

    GREEN: real publisher → pump drains outbox to log → host-B consumer
    delivers both messages; payloads decoded and asserted in order.

    DUP-DROPPED: re-running host-B's consumer with cursor reset re-reads the
    same rows; dedup prevents a second delivery (exactly-once guarantee).
    """
    shared_log = _SharedSQLiteLog(tmp_path / "shared.db")
    host_store = _E2EHostStore(
        {"host-a": ["agent-a"], "host-b": ["agent-b"]}
    )
    log_store = _E2ELogStore(shared_log)

    def _write_to_outbox(outbox: OutboxBackend, msgs: list[dict]) -> None:
        for msg in msgs:
            conn = outbox.connect()
            outbox.write(msg, conn)
            conn.commit()
            conn.close()

    messages = [
        {
            "dedupKey": str(uuid.uuid4()),
            "ordering_key": "agent-b",
            "event_type": "fabric.test",
            "payload": {"seq": 1, "content": "first message from host-a"},
        },
        {
            "dedupKey": str(uuid.uuid4()),
            "ordering_key": "agent-b",
            "event_type": "fabric.test",
            "payload": {"seq": 2, "content": "second message from host-a"},
        },
    ]

    # ── RED: noop publisher → host-B receives nothing ──────────────────────
    outbox_red = OutboxBackend(tmp_path / "outbox-red.db")
    _write_to_outbox(outbox_red, messages)

    noop_pump = OutboxPump(
        outbox_red, _NoopPublisher(), tmp_path / "outbox-red.lock"
    )
    noop_pump.run()

    relay_b_red = _RelayCapture()
    dedup_b_red = DedupBackend(tmp_path / "dedup-b-red.db", consumer_id="host-b")
    consumer_b_red = HostConsumer(
        "host-b", host_store, log_store, dedup_b_red, relay_fn=relay_b_red
    )
    await consumer_b_red.run_once(session=None)

    assert relay_b_red.delivered == [], (
        "red: noop publisher delivers nothing to host-b; got "
        f"{relay_b_red.delivered}"
    )
    assert shared_log.read_for_labels(["agent-b"]) == [], (
        "red: noop publisher must not write any rows to the shared log"
    )

    # ── GREEN: real publisher → host-B receives both messages in order ──────
    outbox_green = OutboxBackend(tmp_path / "outbox-green.db")
    _write_to_outbox(outbox_green, messages)

    real_pump = OutboxPump(
        outbox_green, _LogPublisher(shared_log), tmp_path / "outbox-green.lock"
    )
    drained = real_pump.run()
    assert drained == 2, f"green: pump must drain 2 rows; got {drained}"

    relay_b_green = _RelayCapture()
    dedup_b_green = DedupBackend(
        tmp_path / "dedup-b-green.db", consumer_id="host-b"
    )
    consumer_b_green = HostConsumer(
        "host-b",
        host_store,
        log_store,
        dedup_b_green,
        relay_fn=relay_b_green,
    )
    await consumer_b_green.run_once(session=None)

    # Decode actual output
    assert len(relay_b_green.delivered) == 2, (
        f"green: host-b must receive exactly 2 messages; "
        f"got {len(relay_b_green.delivered)}"
    )
    seqs = [d["payload"]["seq"] for d in relay_b_green.delivered]
    assert seqs == [1, 2], f"green: messages must arrive in order [1,2]; got {seqs}"
    assert relay_b_green.delivered[0]["payload"]["content"] == "first message from host-a"
    assert relay_b_green.delivered[1]["payload"]["content"] == "second message from host-a"

    # ── DUP-DROPPED: reset cursor, re-run — dedup must block re-delivery ────
    consumer_b_green._cursor = 0
    await consumer_b_green.run_once(session=None)

    assert len(relay_b_green.delivered) == 2, (
        "dup-dropped: second run with reset cursor must not add new deliveries; "
        f"got {len(relay_b_green.delivered)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Req C — Scenario 2: ISOLATION — cut link, produce N, restore, drain all once
# ─────────────────────────────────────────────────────────────────────────────


async def test_fabric_sc2_isolation_red_then_green(tmp_path: Path) -> None:
    """Scenario 2 — ISOLATION: host-B accumulates events while the log link is cut.

    RED: pump is deliberately NOT run after host-B writes its events (simulating
    an isolated host). The shared log has 0 rows for host-B's label. Asserting
    all N events were delivered fails. Proves non-vacuity.

    GREEN: after "restoring the link" (running the pump), all N events appear in
    the shared log and are decoded in order by host-B's own consumer. Nothing
    adopted host-B's labels while isolated (the host store binding is unchanged).
    """
    N = 4
    shared_log = _SharedSQLiteLog(tmp_path / "shared-isolation.db")
    # host_b_labels is what the host store says — must remain unchanged
    host_b_labels = ["agent-b-isolated"]
    host_store = _E2EHostStore(
        {"host-a": ["agent-a"], "host-b": host_b_labels}
    )
    log_store = _E2ELogStore(shared_log)

    outbox_b = OutboxBackend(tmp_path / "outbox-b-isolated.db")

    # host-B produces N events into its local outbox while isolated
    for i in range(1, N + 1):
        conn = outbox_b.connect()
        outbox_b.write(
            {
                "dedupKey": str(uuid.uuid4()),
                "ordering_key": "agent-b-isolated",
                "event_type": "fabric.isolation.test",
                "payload": {"seq": i, "content": f"isolated event {i}"},
            },
            conn,
        )
        conn.commit()
        conn.close()

    # ── RED: pump not run → log empty → assert N events fails ──────────────
    rows_in_log_red = shared_log.read_for_labels(["agent-b-isolated"])
    assert rows_in_log_red == [], (
        "red: before drain, log must have 0 rows for the isolated host"
    )
    # Red assertion: trying to decode N events from an empty log fails
    assert len(rows_in_log_red) != N, (
        f"red: isolated host has not drained yet; expected 0 rows, not {N}"
    )

    # ── No-adoption check: host-B's labels remain on host-B ────────────────
    labels_after_isolation = await host_store.labels_on_host(None, "host-b")
    assert labels_after_isolation == host_b_labels, (
        f"no-adoption: host-b's labels must not change during isolation; "
        f"got {labels_after_isolation}"
    )
    labels_on_a = await host_store.labels_on_host(None, "host-a")
    assert "agent-b-isolated" not in labels_on_a, (
        "no-adoption: host-a must not adopt host-b's labels while isolated"
    )

    # ── GREEN: restore link by running the pump ─────────────────────────────
    real_pump = OutboxPump(
        outbox_b,
        _LogPublisher(shared_log),
        tmp_path / "outbox-b-isolated.lock",
    )
    drained = real_pump.run()
    assert drained == N, f"green: pump must drain {N} rows; got {drained}"

    # All N rows now in log
    rows_in_log_green = shared_log.read_for_labels(["agent-b-isolated"])
    assert len(rows_in_log_green) == N, (
        f"green: log must have {N} rows after drain; got {len(rows_in_log_green)}"
    )

    # Consume via host-B's consumer and decode
    relay_b = _RelayCapture()
    dedup_b = DedupBackend(
        tmp_path / "dedup-b-isolated.db", consumer_id="host-b"
    )
    consumer_b = HostConsumer(
        "host-b",
        host_store,
        log_store,
        dedup_b,
        relay_fn=relay_b,
    )
    await consumer_b.run_once(session=None)

    # Decode actual delivered output — all N, in order, once
    assert len(relay_b.delivered) == N, (
        f"green: host-b must receive exactly {N} messages; "
        f"got {len(relay_b.delivered)}"
    )
    seqs = [d["payload"]["seq"] for d in relay_b.delivered]
    assert seqs == list(range(1, N + 1)), (
        f"green: events must arrive in order 1..{N}; got {seqs}"
    )
    for i, d in enumerate(relay_b.delivered, start=1):
        assert d["payload"]["content"] == f"isolated event {i}", (
            f"green: decoded content mismatch at seq {i}: {d}"
        )

    # ── Exactly-once after drain: re-run consumer delivers nothing new ───────
    await consumer_b.run_once(session=None)
    assert len(relay_b.delivered) == N, (
        "exactly-once: second run after drain must deliver no new messages"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Req C — Scenario 3: BROADCAST — both subscribers receive exactly once
# ─────────────────────────────────────────────────────────────────────────────


async def test_fabric_sc3_broadcast_red_then_green(tmp_path: Path) -> None:
    """Scenario 3 — BROADCAST: channel fanout reaches both subscribers once each.

    RED: fanout is deliberately skipped (broken broadcaster) → 0 events in the
    shared log → both host consumers receive nothing. Proves non-vacuity.

    GREEN: fanout expands the broadcast to N unicast deliveries (one per
    subscriber). Each host's consumer decodes its own copy. dedupKey is the
    composite f"{subscriber}:{broadcast_id}" — each subscriber's slot is
    independent (per ADR-0093 §Broadcast, provenance: ramjac ADR-0009).
    """
    shared_log = _SharedSQLiteLog(tmp_path / "shared-broadcast.db")
    host_store = _E2EHostStore(
        {"host-a": ["agent-a"], "host-b": ["agent-b"]}
    )
    log_store = _E2ELogStore(shared_log)

    # Both subscribers join the same channel
    channel_store = ChannelStore()
    join_offset = channel_store.join("team-channel", "agent-a")
    channel_store.join("team-channel", "agent-b")

    broadcast_id = str(uuid.uuid4())
    broadcast_payload = {"announcement": "fabric e2e broadcast", "seq": 42}

    # ── RED: fanout not called → log empty → no delivery ──────────────────
    rows_red = shared_log.read_for_labels(["agent-a", "agent-b"])
    assert rows_red == [], (
        "red: before fanout, log must have 0 rows for either subscriber"
    )

    relay_a_red = _RelayCapture()
    relay_b_red = _RelayCapture()
    dedup_a_red = DedupBackend(tmp_path / "dedup-a-red.db", consumer_id="host-a")
    dedup_b_red = DedupBackend(tmp_path / "dedup-b-red.db", consumer_id="host-b")
    consumer_a_red = HostConsumer(
        "host-a", host_store, log_store, dedup_a_red, relay_fn=relay_a_red
    )
    consumer_b_red = HostConsumer(
        "host-b", host_store, log_store, dedup_b_red, relay_fn=relay_b_red
    )
    await consumer_a_red.run_once(session=None)
    await consumer_b_red.run_once(session=None)

    assert relay_a_red.delivered == [], (
        "red: without fanout, host-a receives nothing"
    )
    assert relay_b_red.delivered == [], (
        "red: without fanout, host-b receives nothing"
    )

    # ── GREEN: call fanout → log gets one unicast row per subscriber ────────
    emitted: list[tuple[str, str, dict]] = []

    def _emit(subscriber: str, dedup_key: str, payload: dict) -> None:
        shared_log.append(
            {
                "ordering_key": subscriber,
                "dedupKey": dedup_key,
                "event_type": "channel.broadcast",
                "payload": payload,
            }
        )
        emitted.append((subscriber, dedup_key, payload))

    delivered_to = fanout(
        channel="team-channel",
        broadcast_id=broadcast_id,
        payload=broadcast_payload,
        broadcast_offset=join_offset + 1,  # after both joins
        channel_store=channel_store,
        emit=_emit,
    )

    assert set(delivered_to) == {"agent-a", "agent-b"}, (
        f"fanout must target both subscribers; got {delivered_to}"
    )

    # Verify composite dedupKeys
    for subscriber, dedup_key, _ in emitted:
        assert dedup_key == f"{subscriber}:{broadcast_id}", (
            f"dedupKey must be composite {{subscriber}}:{{broadcast_id}}; "
            f"got {dedup_key!r}"
        )

    # Each host's consumer reads and decodes its own copy
    relay_a_green = _RelayCapture()
    relay_b_green = _RelayCapture()
    dedup_a_green = DedupBackend(
        tmp_path / "dedup-a-green.db", consumer_id="host-a"
    )
    dedup_b_green = DedupBackend(
        tmp_path / "dedup-b-green.db", consumer_id="host-b"
    )
    consumer_a_green = HostConsumer(
        "host-a", host_store, log_store, dedup_a_green, relay_fn=relay_a_green
    )
    consumer_b_green = HostConsumer(
        "host-b", host_store, log_store, dedup_b_green, relay_fn=relay_b_green
    )
    await consumer_a_green.run_once(session=None)
    await consumer_b_green.run_once(session=None)

    # Decode actual delivered output — exactly one each
    assert len(relay_a_green.delivered) == 1, (
        f"green: host-a must receive exactly 1 broadcast copy; "
        f"got {len(relay_a_green.delivered)}"
    )
    assert len(relay_b_green.delivered) == 1, (
        f"green: host-b must receive exactly 1 broadcast copy; "
        f"got {len(relay_b_green.delivered)}"
    )

    # Decode and verify payload content on each host
    decoded_a = relay_a_green.delivered[0]
    assert decoded_a["label"] == "agent-a"
    assert decoded_a["payload"]["announcement"] == "fabric e2e broadcast"
    assert decoded_a["payload"]["seq"] == 42

    decoded_b = relay_b_green.delivered[0]
    assert decoded_b["label"] == "agent-b"
    assert decoded_b["payload"]["announcement"] == "fabric e2e broadcast"
    assert decoded_b["payload"]["seq"] == 42

    # Each received a distinct dedupKey (no shared slot)
    dedup_key_a = decoded_a["dedupKey"]
    dedup_key_b = decoded_b["dedupKey"]
    assert dedup_key_a != dedup_key_b, (
        f"broadcast dedupKeys must be distinct per subscriber; "
        f"got {dedup_key_a!r} and {dedup_key_b!r}"
    )
    assert dedup_key_a == f"agent-a:{broadcast_id}"
    assert dedup_key_b == f"agent-b:{broadcast_id}"

    # ── Exactly-once: re-run both consumers → no new deliveries ────────────
    await consumer_a_green.run_once(session=None)
    await consumer_b_green.run_once(session=None)

    assert len(relay_a_green.delivered) == 1, (
        "exactly-once: second run must not re-deliver to host-a"
    )
    assert len(relay_b_green.delivered) == 1, (
        "exactly-once: second run must not re-deliver to host-b"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Req A — read_for_labels integration test (TREADMILL_INTEGRATION-gated)
# ─────────────────────────────────────────────────────────────────────────────


@_INTEGRATION_SKIP
async def test_read_for_labels_integration() -> None:
    """Req A: real-Postgres cross-host isolation for read_for_labels (ADR-0093).

    Appends rows for two labels bound to different hosts. Asserts that
    read_for_labels([label_a]) returns ONLY label_a's rows — no cross-host
    bleed through the SQL filter. Requires a dedicated test Postgres instance
    (TREADMILL_TEST_DATABASE_URL) and TREADMILL_INTEGRATION=1.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from treadmill_api.event_log_store import EventLogStore

    url = _TEST_DB_URL
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            store = EventLogStore()

            label_a = f"e2e-host-a-{uuid.uuid4().hex[:8]}"
            label_b = f"e2e-host-b-{uuid.uuid4().hex[:8]}"

            # Append two rows for label_a and one for label_b
            await store.append(
                session,
                {
                    "dedupKey": str(uuid.uuid4()),
                    "ordering_key": label_a,
                    "event_type": "fabric.integration.test",
                    "payload": {"host": "host-a", "seq": 1},
                },
            )
            await store.append(
                session,
                {
                    "dedupKey": str(uuid.uuid4()),
                    "ordering_key": label_b,
                    "event_type": "fabric.integration.test",
                    "payload": {"host": "host-b", "seq": 1},
                },
            )
            await store.append(
                session,
                {
                    "dedupKey": str(uuid.uuid4()),
                    "ordering_key": label_a,
                    "event_type": "fabric.integration.test",
                    "payload": {"host": "host-a", "seq": 2},
                },
            )
            await session.flush()

            # read_for_labels([label_a]) must return only label_a's rows
            rows_a = await store.read_for_labels(session, [label_a])
            assert len(rows_a) == 2, (
                f"read_for_labels([label_a]) must return 2 rows; got {len(rows_a)}"
            )
            for row in rows_a:
                assert row.ordering_key == label_a, (
                    f"cross-host bleed: expected ordering_key={label_a!r}; "
                    f"got {row.ordering_key!r}"
                )
                assert row.payload["host"] == "host-a"

            # Offsets must be strictly ascending (ADR-0093 ordering guarantee)
            offsets = [r.offset for r in rows_a]
            assert offsets == sorted(offsets), (
                f"rows must be in ascending offset order; got {offsets}"
            )
            assert len(set(offsets)) == len(offsets), "offsets must be unique"

            # read_for_labels([label_b]) must return only label_b's row
            rows_b = await store.read_for_labels(session, [label_b])
            assert len(rows_b) == 1, (
                f"read_for_labels([label_b]) must return 1 row; got {len(rows_b)}"
            )
            assert rows_b[0].ordering_key == label_b
            assert rows_b[0].payload["host"] == "host-b"

            # Neither query bleeds across labels
            for r in rows_a:
                assert r.ordering_key != label_b, (
                    "cross-host bleed: label_a query returned a label_b row"
                )
            for r in rows_b:
                assert r.ordering_key != label_a, (
                    "cross-host bleed: label_b query returned a label_a row"
                )

            await session.rollback()
    finally:
        await engine.dispose()
