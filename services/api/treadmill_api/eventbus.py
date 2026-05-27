"""Event publisher.

Per ADR-0011, the API publishes typed events on the bus so consumers (the
rule engine, autoscaler, observability taps) can subscribe via filtered
SQS queues. Two implementations:

  * ``SNSEventPublisher`` — boto3 SNS publish wrapped in
    ``asyncio.to_thread`` so it doesn't block the event loop. Writes
    message attributes for ``entity_type``, ``action``, and ``task_id``
    (when present), enabling consumers to attach SQS subscription filters.
  * ``LoggingEventPublisher`` — stderr fallback when ``EVENTS_TOPIC_ARN`` is
    unset (local dev / tests / subprocesses without AWS access).

Persistence is the source of truth; the bus is notification only. A
failed publish does not roll back the persisted Event row — the caller
catches and logs.

Failure modes are *typed* so the dispatcher can route them differently:

  * ``PublishError`` — the transport (SNS / boto3) rejected the publish.
    Caller persists a ``dispatch_publish_failed`` marker and the replay
    loop retries per the 2026-05-11 closure plan (decision #3).
  * ``pydantic.ValidationError`` — the typed payload itself is malformed.
    This is a programming bug, not a transient failure; it propagates
    unchanged so the dispatcher fails the request rather than enqueueing
    a retry that would just fail again.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from botocore.exceptions import ClientError

from treadmill_api.events import EventPayload, encode_payload
from treadmill_api.models import Event
from treadmill_api.observability import inject_trace_context

logger = logging.getLogger("treadmill.eventbus")


# ── In-process broadcast for live consumers (dashboard WS, ADR-0056) ──────────
#
# The publishers below fan every record they construct out to in-process
# subscribers in addition to their SNS / log target. Subscribers register
# via ``subscribe_local`` and receive a bounded ``asyncio.Queue`` of event
# records (shape: ``_build_record``). Backpressure drops on full queues —
# a wedged WS client must never block the publish path.
#
# We capture each subscriber's running loop at registration time and
# schedule queue puts via ``loop.call_soon_threadsafe``. That keeps
# broadcast correct when the publisher runs on a different thread than
# the consumer (e.g. tests where Starlette's ``TestClient`` portal runs
# the WS handler on a worker thread).

_local_subscribers: "dict[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop]" = {}


def subscribe_local() -> "asyncio.Queue[dict[str, Any]]":
    """Register an in-process subscriber to the publish stream.

    Returns a bounded ``asyncio.Queue`` onto which every record handed to
    ``EventPublisher.publish`` is pushed. The caller MUST call
    ``unsubscribe_local(queue)`` when done, and MUST call this from
    inside a running event loop (the loop on which the consumer will
    ``await queue.get()``).
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
    _local_subscribers[q] = loop
    return q


def unsubscribe_local(queue: "asyncio.Queue[dict[str, Any]]") -> None:
    _local_subscribers.pop(queue, None)


def _put_nowait_drop(
    queue: "asyncio.Queue[dict[str, Any]]", record: dict[str, Any],
) -> None:
    try:
        queue.put_nowait(record)
    except asyncio.QueueFull:
        # The WS handler tracks its own send-side backpressure; we just
        # drop here so the publish path stays non-blocking.
        pass


def _broadcast_local(record: dict[str, Any]) -> None:
    """Fan ``record`` out to every in-process subscriber.

    Safe to call from any thread — each subscriber is woken via
    ``call_soon_threadsafe`` on the loop it registered against. Closed
    loops drop their subscriber so a stale registration can't keep us
    looping forever.
    """
    for queue, loop in list(_local_subscribers.items()):
        try:
            loop.call_soon_threadsafe(_put_nowait_drop, queue, record)
        except RuntimeError:
            # Loop is closed — clean up the stale subscriber.
            _local_subscribers.pop(queue, None)


class PublishError(Exception):
    """The event-bus transport rejected a publish.

    Raised by ``SNSEventPublisher.publish`` when the underlying boto3
    client raises ``botocore.exceptions.ClientError``. Distinct from a
    Pydantic ``ValidationError`` (which signals a malformed payload, not
    a transport failure) so callers can route the two differently.
    """


def _build_record(event: Event, typed_payload: EventPayload) -> dict[str, Any]:
    return {
        "event_id": str(event.id),
        "entity_type": event.entity_type,
        "action": event.action,
        "task_id": str(event.task_id) if event.task_id is not None else None,
        "plan_id": str(event.plan_id) if event.plan_id is not None else None,
        "run_id": str(event.run_id) if event.run_id is not None else None,
        "step_id": str(event.step_id) if event.step_id is not None else None,
        "payload": encode_payload(typed_payload),
    }


def _build_attributes(event: Event) -> dict[str, dict[str, str]]:
    """SNS MessageAttributes for filtered SQS subscriptions per ADR-0007."""
    attrs: dict[str, dict[str, str]] = {
        "entity_type": {"DataType": "String", "StringValue": event.entity_type},
        "action": {"DataType": "String", "StringValue": event.action},
    }
    if event.task_id is not None:
        attrs["task_id"] = {"DataType": "String", "StringValue": str(event.task_id)}
    attrs.update(inject_trace_context())
    return attrs


class EventPublisher(Protocol):
    async def publish(self, event: Event, typed_payload: EventPayload) -> None: ...


class LoggingEventPublisher:
    """Fallback when no SNS topic is configured. Writes a structured INFO
    log line per published event."""

    async def publish(self, event: Event, typed_payload: EventPayload) -> None:
        record = _build_record(event, typed_payload)
        _broadcast_local(record)
        logger.info(
            "event published (log-only): entity=%s action=%s id=%s task_id=%s",
            event.entity_type,
            event.action,
            event.id,
            event.task_id,
            extra={"event_record": record},
        )


class SNSEventPublisher:
    """SNS-backed publisher. The boto3 SNS client is sync; we wrap calls
    in ``asyncio.to_thread`` so they don't block the event loop."""

    def __init__(self, sns_client: Any, topic_arn: str) -> None:
        self.sns_client = sns_client
        self.topic_arn = topic_arn

    async def publish(self, event: Event, typed_payload: EventPayload) -> None:
        # ``_build_record`` calls ``encode_payload`` which may raise
        # ``pydantic.ValidationError`` if ``typed_payload`` is malformed.
        # That is a programming bug, not a transport failure — let it
        # propagate unchanged so callers can distinguish the two cases.
        record = _build_record(event, typed_payload)
        attributes = _build_attributes(event)
        # Fan out to in-process subscribers BEFORE the SNS hop so a
        # transport failure doesn't starve local consumers (the dashboard
        # WS, ADR-0056) of records the Event row already committed to.
        _broadcast_local(record)
        try:
            await asyncio.to_thread(
                self.sns_client.publish,
                TopicArn=self.topic_arn,
                Message=json.dumps(record),
                MessageAttributes=attributes,
            )
        except ClientError as exc:
            # Wrap transport failures so the dispatcher can persist a
            # ``dispatch_publish_failed`` marker and let the replay loop
            # retry. The original exception is chained for diagnosis.
            raise PublishError(
                f"SNS publish failed for {event.entity_type}.{event.action} "
                f"(event_id={event.id}, topic={self.topic_arn}): {exc}"
            ) from exc
        logger.debug(
            "event published to SNS: entity=%s action=%s id=%s topic=%s",
            event.entity_type,
            event.action,
            event.id,
            self.topic_arn,
        )


def make_publisher(settings: Any, sns_client: Any | None) -> EventPublisher:
    """Build the right publisher based on configuration.

    If ``EVENTS_TOPIC_ARN`` is set and ``sns_client`` is provided, the SNS
    publisher is used. Otherwise we fall back to the logging publisher.
    """
    if settings.events_topic_arn and sns_client is not None:
        return SNSEventPublisher(sns_client, settings.events_topic_arn)
    return LoggingEventPublisher()


# Module-level publisher accessor — set by the lifespan handler at startup.
_publisher: EventPublisher | None = None


def set_publisher(publisher: EventPublisher) -> None:
    global _publisher
    _publisher = publisher


def get_publisher() -> EventPublisher:
    """Return the configured publisher. Falls back to the logging publisher
    if no one set one (which happens in test contexts that bypass the
    lifespan handler)."""
    if _publisher is None:
        return LoggingEventPublisher()
    return _publisher
