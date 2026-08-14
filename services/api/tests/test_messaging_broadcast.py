"""Tests for treadmill_api/messaging/broadcast.py (ADR-0093 §Broadcast).

Four invariants, each proven non-vacuous with foils where required.
Sandbox-safe: fake log (in-memory ChannelStore) + injected emit spy; no network.

  ONCE PER SUBSCRIBER — composite dedupKey delivers each subscriber exactly once.
    Foil (a): fresh-UUID dedupKey → re-fanout reprocesses (broken).
    Foil (b): broadcast_id-alone dedupKey + shared dedup → only first subscriber wins (broken).
    Green: composite key + per-subscriber dedup → each delivered exactly once, re-fanout blocked.

  ISOLATED-AT-BROADCAST — offline subscriber gets the delivery (subscribe set fixed at offset).

  JOIN AFTER — subscriber who joined after broadcast_offset misses it.

  LEAVE AFTER — subscriber who left after broadcast_offset still gets it.

Provenance: broadcast rules from ramjac ADR-0009, ported (task dc40b385, ADR-0093).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from treadmill_api.messaging.broadcast import ChannelStore, fanout
from treadmill_api.messaging.dedup import ClaimResult, DedupBackend


# ── Broken fanout variants (foils) ────────────────────────────────────────────


def _fresh_uuid_fanout(
    channel: str,
    broadcast_id: str,
    payload: dict,
    broadcast_offset: int,
    channel_store: ChannelStore,
    emit: Any,
) -> list[str]:
    """BROKEN variant — generates a fresh UUID as dedupKey for every delivery.

    On re-fanout the new UUID is unknown to the receiver's dedup backend:
    the claim is granted again and the handler re-runs — reprocessed on re-fanout.

    Provenance: foil (a), ADR-0093 §Broadcast, task dc40b385.
    """
    subscribers = channel_store.subscribers_at(channel, broadcast_offset)
    delivered = []
    for subscriber in sorted(subscribers):
        dedup_key = str(uuid.uuid4())  # BUG: fresh UUID, not composite
        emit(subscriber, dedup_key, payload)
        delivered.append(subscriber)
    return delivered


def _event_id_alone_fanout(
    channel: str,
    broadcast_id: str,
    payload: dict,
    broadcast_offset: int,
    channel_store: ChannelStore,
    emit: Any,
) -> list[str]:
    """BROKEN variant — uses broadcast_id alone (no subscriber prefix) as dedupKey.

    When a shared dedup backend commits broadcast_id for the first subscriber,
    subsequent subscribers find it DUPLICATE — only the first to dedup wins.
    This violates the per-subscriber delivery rule (ADR-0093; ramjac ADR-0009 §3).

    Provenance: foil (b), ADR-0093 §Broadcast, task dc40b385.
    """
    subscribers = channel_store.subscribers_at(channel, broadcast_offset)
    delivered = []
    for subscriber in sorted(subscribers):
        dedup_key = broadcast_id  # BUG: no subscriber prefix
        emit(subscriber, dedup_key, payload)
        delivered.append(subscriber)
    return delivered


# ── Helpers ───────────────────────────────────────────────────────────────────


def _dedup_receiver(dedup: DedupBackend, collected: list[str]) -> Any:
    """Return an emit spy that claims, records, and commits using dedup."""
    def receive(subscriber: str, dedup_key: str, payload: dict) -> None:
        now = datetime.now(timezone.utc)
        result = dedup.claim(dedup_key, now, timedelta(minutes=5))
        if result == ClaimResult.GRANTED:
            collected.append(subscriber)
            dedup.commit(dedup_key)
    return receive


# ══════════════════════════════════════════════════════════════════════════════
# ONCE PER SUBSCRIBER
# ══════════════════════════════════════════════════════════════════════════════


def test_red_a_fresh_uuid_redelivers_on_refanout(tmp_path: Path) -> None:
    """RED (foil-a): fresh UUID dedupKey → re-fanout reprocesses alice.

    Each fanout call generates a new UUID. The receiver's dedup backend has
    never seen it → GRANTED → handler runs again. Re-fanout reprocesses.

    Provenance: foil (a), ADR-0093 §Broadcast, task dc40b385.
    """
    store = ChannelStore()
    store.join("team", "alice")
    broadcast_offset = 0

    alice_dedup = DedupBackend(tmp_path / "alice.db", consumer_id="alice")
    delivered: list[str] = []
    emit = _dedup_receiver(alice_dedup, delivered)

    # First fanout — alice delivered (key uuid1 → GRANTED)
    _fresh_uuid_fanout("team", "b1", {}, broadcast_offset, store, emit)
    # Re-fanout (crash recovery) — alice delivered AGAIN (key uuid2 → GRANTED)
    _fresh_uuid_fanout("team", "b1", {}, broadcast_offset, store, emit)

    # RED assertion: fresh UUID causes double-delivery on re-fanout
    assert delivered.count("alice") == 2, (
        "RED foil-a: fresh UUID per delivery — re-fanout re-delivers alice. "
        "If count is 1, the foil is no longer broken — check the test."
    )


def test_red_b_event_id_alone_first_to_dedup_wins(tmp_path: Path) -> None:
    """RED (foil-b): broadcast_id-alone dedupKey + shared dedup → only alice wins.

    alice (sorted first) claims and commits broadcast_id. bob then claims
    the same key against the same shared dedup backend → DUPLICATE → skipped.
    Only the first-to-dedup subscriber receives the broadcast.

    Provenance: foil (b), ADR-0093 §Broadcast, task dc40b385.
    """
    store = ChannelStore()
    store.join("team", "alice")
    store.join("team", "bob")
    broadcast_offset = 1  # after both joins

    # Shared dedup — the foil: all subscribers share one consumer_id
    shared_dedup = DedupBackend(tmp_path / "shared.db", consumer_id="global")
    delivered: list[str] = []
    emit = _dedup_receiver(shared_dedup, delivered)

    _event_id_alone_fanout("team", "b1", {}, broadcast_offset, store, emit)

    # RED assertion: only alice (first in sorted order) delivered; bob blocked
    assert delivered == ["alice"], (
        "RED foil-b: broadcast_id-alone + shared dedup — only the first subscriber "
        "wins; bob is blocked as DUPLICATE. "
        f"Got {delivered}"
    )
    assert "bob" not in delivered, (
        "RED foil-b: bob must be blocked by the shared dedup — first-to-dedup-wins"
    )


def test_green_composite_dedup_once_per_subscriber_re_fanout_blocked(
    tmp_path: Path,
) -> None:
    """GREEN (once-per-subscriber): composite f"{subscriber}:{broadcast_id}" +
    per-subscriber DedupBackend → each subscriber delivered exactly once; a
    second fanout (re-fanout, crash recovery) is blocked by dedup.

    Provenance: ADR-0093 §Broadcast rules 1–3, task dc40b385.
    """
    store = ChannelStore()
    store.join("team", "alice")
    store.join("team", "bob")
    broadcast_offset = 1  # after both joins

    alice_dedup = DedupBackend(tmp_path / "alice.db", consumer_id="alice")
    bob_dedup = DedupBackend(tmp_path / "bob.db", consumer_id="bob")
    delivered: list[str] = []

    def emit(subscriber: str, dedup_key: str, payload: dict) -> None:
        dedup = alice_dedup if subscriber == "alice" else bob_dedup
        now = datetime.now(timezone.utc)
        result = dedup.claim(dedup_key, now, timedelta(minutes=5))
        if result == ClaimResult.GRANTED:
            delivered.append(subscriber)
            dedup.commit(dedup_key)

    # First fanout — both subscribers delivered
    fanout("team", "b1", {}, broadcast_offset, store, emit)
    assert set(delivered) == {"alice", "bob"}, (
        "both subscribers must be delivered on first fanout"
    )

    # Re-fanout (crash recovery) — both blocked by dedup
    fanout("team", "b1", {}, broadcast_offset, store, emit)
    assert delivered.count("alice") == 1, "alice must not be re-delivered on re-fanout"
    assert delivered.count("bob") == 1, "bob must not be re-delivered on re-fanout"


# ══════════════════════════════════════════════════════════════════════════════
# ISOLATED-AT-BROADCAST
# ══════════════════════════════════════════════════════════════════════════════


def test_isolated_at_broadcast_subscriber_gets_delivery_on_drain() -> None:
    """Offline subscriber at broadcast time gets the delivery — the subscriber set
    is fixed at broadcast_offset, not "currently active."

    This is the ADR-0094 drain-on-reconnect property applied to broadcast:
    alice was subscribed at broadcast_offset; her delivery is emitted to the log;
    she receives it when her consumer drains (HostConsumer.run_once).

    Here we verify that fanout() emits for alice regardless of her online status.
    The "drain" step is the host consumer's responsibility (tested separately in
    test_messaging_host_consumer.py). Here we assert the emission happens.
    """
    store = ChannelStore()
    alice_join_offset = store.join("team", "alice")  # offset 0
    broadcast_offset = alice_join_offset              # broadcast at the join offset

    emitted: list[tuple[str, str, dict]] = []

    # alice is "offline" — no consumer running. fanout() still emits for her.
    fanout(
        "team", "b1", {"hello": "world"}, broadcast_offset, store,
        lambda sub, key, pay: emitted.append((sub, key, pay)),
    )

    delivered_subscribers = [sub for sub, _, _ in emitted]
    assert "alice" in delivered_subscribers, (
        "isolated-at-broadcast: fanout must emit for alice even while she is offline; "
        "she receives the delivery when she drains the log"
    )
    assert len(emitted) == 1, "exactly one emission for the single subscriber"
    assert emitted[0][1] == "alice:b1", (
        "composite dedupKey must be used: f'{subscriber}:{broadcast_id}'"
    )


# ══════════════════════════════════════════════════════════════════════════════
# JOIN AFTER
# ══════════════════════════════════════════════════════════════════════════════


def test_join_after_broadcast_offset_misses_broadcast() -> None:
    """Subscriber joins AFTER the broadcast offset → not in fold → misses it.

    ADR-0093: "A join after that point misses this broadcast."
    The subscriber set is the fold at broadcast_offset; events at later offsets
    are not included.
    """
    store = ChannelStore()
    bob_offset = store.join("team", "bob")   # offset 0 — bob is in the fold
    broadcast_offset = bob_offset             # fold = {bob}
    store.join("team", "alice")              # offset 1 — alice joins AFTER broadcast

    emitted: list[str] = []
    fanout("team", "b1", {}, broadcast_offset, store,
           lambda sub, key, pay: emitted.append(sub))

    assert "alice" not in emitted, (
        "alice joined after broadcast_offset — she must miss this broadcast"
    )
    assert "bob" in emitted, (
        "bob was subscribed at broadcast_offset — he must receive it"
    )


# ══════════════════════════════════════════════════════════════════════════════
# LEAVE AFTER
# ══════════════════════════════════════════════════════════════════════════════


def test_leave_after_broadcast_does_not_un_deliver() -> None:
    """Subscriber leaves AFTER the broadcast offset → was in fold → delivery emitted.

    ADR-0093: "a leave after it does not un-deliver."
    alice was subscribed at broadcast_offset; she leaves afterward. The fold at
    broadcast_offset still includes her, so her delivery is emitted.
    """
    store = ChannelStore()
    alice_join_offset = store.join("team", "alice")  # offset 0
    broadcast_offset = alice_join_offset               # fold = {alice}
    store.leave("team", "alice")                       # offset 1 — leaves AFTER

    emitted: list[str] = []
    fanout("team", "b1", {}, broadcast_offset, store,
           lambda sub, key, pay: emitted.append(sub))

    assert "alice" in emitted, (
        "alice was subscribed at broadcast_offset — her delivery must be emitted "
        "even though she left afterward (leave does not un-deliver)"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Subscriber-set fold unit tests
# ══════════════════════════════════════════════════════════════════════════════


def test_fold_join_then_leave_before_offset_gives_empty() -> None:
    """A subscriber who joined then left before the offset is not in the fold."""
    store = ChannelStore()
    store.join("chan", "alice")   # offset 0
    store.leave("chan", "alice")  # offset 1

    # fold at offset 1: alice joined at 0, left at 1 — not subscribed
    subs = store.subscribers_at("chan", up_to_offset=1)
    assert "alice" not in subs


def test_fold_channel_isolation() -> None:
    """Events on channel A do not affect the subscriber set for channel B."""
    store = ChannelStore()
    store.join("chan-a", "alice")  # offset 0
    store.join("chan-b", "bob")    # offset 1

    subs_a = store.subscribers_at("chan-a", up_to_offset=1)
    subs_b = store.subscribers_at("chan-b", up_to_offset=1)

    assert "alice" in subs_a and "bob" not in subs_a
    assert "bob" in subs_b and "alice" not in subs_b


def test_fanout_payload_forwarded_unchanged() -> None:
    """fanout() forwards the payload dict to emit unchanged."""
    store = ChannelStore()
    store.join("team", "alice")
    received_payloads: list[dict] = []
    payload = {"key": "value", "seq": 42}

    fanout("team", "b1", payload, 0, store,
           lambda sub, key, pay: received_payloads.append(pay))

    assert received_payloads == [payload], "payload must be forwarded unchanged to emit"
