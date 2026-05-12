"""Internal Treadmill-control-plane events.

These events are emitted by the API itself to record its own operational
state — they have no external producer (no worker, no webhook, no CLI
submission produces them). They live under ``entity_type='_internal'``
to keep them distinct from the entity-lifecycle events the rest of the
system reasons about.

Two inhabitants compose the dispatch-publish replay protocol (A.8/A.10
in the 2026-05-11 closure plan):

  * ``DispatchPublishFailed`` — written by the dispatcher when its SNS
    publish or SQS work-queue send raises. Carries the id of the
    original Event row so the replay loop can re-issue the publish on a
    slow tick.

  * ``DispatchPublishReplayed`` — written by the replay loop when a
    failure marker is successfully re-published. The events table is
    append-only by convention; emitting a sibling row preserves history
    rather than mutating the marker payload in place. The replay loop
    treats a marker as resolved iff it has a matching replayed sibling.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar, Literal

from treadmill_api.events.base import EventPayload


class DispatchPublishFailed(EventPayload):
    """Marker that the dispatcher failed to deliver an Event to the bus.

    Persisted in the events table alongside (but distinct from) the
    original Event whose publish failed. The replay loop reads these
    markers, looks up the referenced original event by id, and
    re-attempts the publish on a 30s tick (A.10).

    ``target`` distinguishes the SNS audit-bus publish failure from the
    SQS work-queue send failure — the replay loop handles each with a
    different downstream call.
    """

    ENTITY_TYPE: ClassVar[str] = "_internal"
    ACTION: ClassVar[str] = "dispatch_publish_failed"

    original_event_id: uuid.UUID
    """The id of the Event row whose publish/send failed. The replay loop
    SELECTs the original row by this id and re-emits its payload."""

    target: Literal["sns", "sqs"]
    """Which downstream the dispatcher tried to reach. ``sns`` = the events
    SNS topic (audit fan-out). ``sqs`` = the FIFO work queue (worker claim).
    Different replay paths for each — see A.10."""

    error_message: str
    """The exception's string representation, truncated. For human
    debugging only; the replay loop does not branch on this field."""

    attempted_at: datetime
    """When the publish/send was attempted. Used by the replay loop to
    rate-limit re-attempts and to surface dispatch-publish staleness in
    ops dashboards."""


class DispatchPublishReplayed(EventPayload):
    """Marker that a previously-failed dispatch publish was healed.

    Inserted by the replay loop after a successful re-issue of the
    original event. The presence of one of these rows referencing a
    given ``DispatchPublishFailed`` marker is the signal the loop uses
    to skip that marker on subsequent ticks — the marker payload itself
    is never mutated (events are append-only).

    ``original_failure_event_id`` points at the marker's own Event row
    id (i.e. the failed-marker row in the ``events`` table) so the
    resolution chain is explicit in the audit log. ``original_event_id``
    duplicates the marker's reference to the *originally-failing* event
    for convenience — saves the replay loop one join when scanning.
    """

    ENTITY_TYPE: ClassVar[str] = "_internal"
    ACTION: ClassVar[str] = "dispatch_publish_replayed"

    original_failure_event_id: uuid.UUID
    """The id of the ``DispatchPublishFailed`` Event row this resolution
    pairs with. Unique per resolution — the replay loop SELECTs markers
    whose id is not in the set of ``original_failure_event_id`` values."""

    original_event_id: uuid.UUID
    """The id of the *originally-failing* Event row (carried forward
    from the marker's ``original_event_id`` for convenience)."""

    replayed_at: datetime
    """When the replay loop successfully re-published. Useful for
    measuring failure → recovery latency in ops dashboards."""
