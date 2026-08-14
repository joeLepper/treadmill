"""Channel subscription + broadcast delivery (ADR-0093 §Broadcast).

Broadcast is N unicast deliveries — one per subscriber — honoring three rules:

  1. Subscriber set = the log fold at the broadcast's offset.
     "Current subscribers" means the set as of the broadcast's append point —
     deterministic and replayable. A join after that offset misses the broadcast;
     a leave after it does not un-deliver.

  2. dedupKey = composite f"{subscriber}:{broadcast_id}" — NEVER a fresh UUID.
     A fresh UUID per delivery would let a re-fanned-out broadcast reprocess. A
     broadcast_id alone (no subscriber prefix) would let the first subscriber to
     dedup block all others (ADR-0093 §Decision; provenance: ramjac ADR-0009).

  3. Order is per-subscriber (ordering_key = subscriber label), never per-channel.
     Two subscribers may see the same broadcast at different positions in their own
     mail; that is expected (ADR-0093 Property 2: ordering key is the recipient).

Channel subscription is a durable fact: join/leave events carry offsets. The
subscriber set at any broadcast offset is the fold of those events up to that point.

emit() is injected so callers may use any log backend (outbox, EventLogStore,
test spy). In production emit() appends a unicast event to the central log;
HostConsumer then delivers it to the local session via cc-relay (ADR-0094).

Provenance: broadcast rules from ramjac ADR-0009, ported to Treadmill's
messaging library (task dc40b385). See ADR-0093 §Broadcast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class SubscriptionEvent:
    """One join or leave event on the subscription log.

    offset: monotonically increasing append position.
    action: "join" or "leave".
    """
    offset: int
    channel: str
    action: str
    subscriber: str


def _fold_subscribers(
    channel: str,
    events: list[SubscriptionEvent],
    up_to_offset: int,
) -> frozenset[str]:
    """Compute subscriber set by folding events up to (inclusive) up_to_offset.

    Events are processed in offset order. Only events for the given channel
    are considered. Join adds a subscriber; leave removes one.
    """
    subscribers: set[str] = set()
    for ev in sorted(events, key=lambda e: e.offset):
        if ev.offset > up_to_offset:
            break
        if ev.channel != channel:
            continue
        if ev.action == "join":
            subscribers.add(ev.subscriber)
        elif ev.action == "leave":
            subscribers.discard(ev.subscriber)
    return frozenset(subscribers)


class ChannelStore:
    """Records channel subscription events and computes subscriber sets.

    Subscriptions are durable facts on the log: each join/leave is a
    SubscriptionEvent with a monotonically increasing offset. The subscriber
    set at any broadcast offset is the fold of all events up to that point.

    In production: wrap EventLogStore (recording channel.join / channel.leave
    event_type rows). In tests: use directly — the in-memory list is the log.
    """

    def __init__(self, events: list[SubscriptionEvent] | None = None) -> None:
        self._events: list[SubscriptionEvent] = list(events or [])
        self._next_offset: int = (
            max((e.offset for e in self._events), default=-1) + 1
        )

    def join(self, channel: str, subscriber: str) -> int:
        """Record a join event. Returns the offset assigned to this event."""
        offset = self._next_offset
        self._events.append(SubscriptionEvent(offset, channel, "join", subscriber))
        self._next_offset += 1
        return offset

    def leave(self, channel: str, subscriber: str) -> int:
        """Record a leave event. Returns the offset assigned to this event."""
        offset = self._next_offset
        self._events.append(SubscriptionEvent(offset, channel, "leave", subscriber))
        self._next_offset += 1
        return offset

    def subscribers_at(self, channel: str, up_to_offset: int) -> frozenset[str]:
        """Return the subscriber set at (inclusive) the given offset."""
        return _fold_subscribers(channel, self._events, up_to_offset)


def fanout(
    channel: str,
    broadcast_id: str,
    payload: dict[str, Any],
    broadcast_offset: int,
    channel_store: ChannelStore,
    emit: Callable[[str, str, dict[str, Any]], None],
) -> list[str]:
    """Expand a broadcast to N unicast deliveries (ADR-0093 §Broadcast).

    Three rules:
    1. Subscriber set = fold at broadcast_offset (not "current subscribers").
    2. dedupKey = f"{subscriber}:{broadcast_id}" — composite, per-subscriber.
    3. Each delivery uses ordering_key=subscriber (per-subscriber order).

    Calls emit(subscriber, dedup_key, payload) for each subscriber in the fold.
    emit is injected: in production it appends a unicast event to the central
    log; in tests it captures calls for assertion.

    Returns the sorted list of subscriber labels emitted to.
    """
    subscribers = channel_store.subscribers_at(channel, broadcast_offset)
    delivered: list[str] = []
    for subscriber in sorted(subscribers):
        dedup_key = f"{subscriber}:{broadcast_id}"
        emit(subscriber, dedup_key, payload)
        delivered.append(subscriber)
    return delivered
