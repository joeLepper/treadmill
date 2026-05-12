"""Replay loop for failed dispatch publishes.

When the dispatcher's SNS publish fails, it persists a
``DispatchPublishFailed`` Event row as a durable marker (A.8). This
module's ``ReplayLoop`` polls for those markers on a slow tick and
re-issues the publish; on success it writes a sibling
``DispatchPublishReplayed`` row that marks the marker as resolved.

Why the sibling-event design (rather than mutating the marker payload)?
The events table is append-only by convention — every other consumer of
the audit log treats it as immutable. A "resolved" sibling preserves
that invariant *and* yields a natural failure → recovery latency metric
(the time between the two events). The cost is the unresolved-marker
query gets a ``WHERE NOT EXISTS`` subquery; we accept it.

The replay loop mirrors ``CoordinationConsumer``'s lifecycle (``start`` /
``stop``) so the lifespan handler can manage both with the same pattern.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from treadmill_api.eventbus import EventPublisher
from treadmill_api.events.internal import (
    DispatchPublishFailed,
    DispatchPublishReplayed,
)
from treadmill_api.events.registry import (
    UnknownEventTypeError,
    parse_payload,
)
from treadmill_api.models import Event

logger = logging.getLogger("treadmill.replay")


# How many markers to process per tick. Bounded so a backlog can't
# monopolize one tick; the next tick picks up where this one left off.
_BATCH_SIZE = 50


class ReplayLoop:
    """Background task that heals failed dispatch publishes.

    Constructor takes a ``publisher`` + ``sessionmaker`` so tests can
    feed in fakes without spinning up the full app. ``tick_seconds``
    defaults to 30s — slow enough that a transient SNS hiccup has time
    to clear before we re-issue, fast enough that a real outage doesn't
    leave the audit log lagging for long.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        sessionmaker: async_sessionmaker[AsyncSession],
        tick_seconds: float = 30.0,
    ) -> None:
        self.publisher = publisher
        self.sessionmaker = sessionmaker
        self.tick_seconds = tick_seconds
        self._stopped = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="dispatch-replay-loop")
        logger.info(
            "dispatch replay loop started: tick_seconds=%s", self.tick_seconds
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("dispatch replay loop raised on shutdown")
            self._task = None
        logger.info("dispatch replay loop stopped")

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "dispatch replay loop tick raised; will retry next tick"
                )
            try:
                await asyncio.sleep(self.tick_seconds)
            except asyncio.CancelledError:
                raise

    # ── Public tick (callable from tests) ─────────────────────────────────────

    async def tick(self) -> int:
        """Run one tick: scan markers, replay, write resolution rows.

        Returns the number of markers successfully replayed on this tick.
        Tests drive this directly so they don't have to manage the
        ``asyncio.sleep`` cadence.
        """
        async with self.sessionmaker() as session:
            markers = await self._fetch_unresolved_markers(session)
            replayed = 0
            for marker_row in markers:
                if await self._replay_one(session, marker_row):
                    replayed += 1
            await session.commit()
            if replayed:
                logger.info(
                    "dispatch replay loop healed %d markers this tick", replayed
                )
            return replayed

    async def _fetch_unresolved_markers(
        self, session: AsyncSession
    ) -> list[Any]:
        """SELECT ``dispatch_publish_failed`` markers without a matching
        ``dispatch_publish_replayed`` sibling.

        Ordering by ``created_at`` so the oldest failures heal first —
        keeps replay-latency tail bounded.
        """
        # Raw SQL because the predicate uses a JSONB ->> extraction on the
        # *replayed* sibling's ``original_failure_event_id`` and a NOT
        # EXISTS subquery — both are awkward in pure SQLAlchemy Core for
        # this niche table.
        sql = text(
            """
            SELECT id, entity_type, action, plan_id, task_id, run_id,
                   step_id, payload, created_at
            FROM events f
            WHERE f.entity_type = '_internal'
              AND f.action = 'dispatch_publish_failed'
              AND NOT EXISTS (
                SELECT 1 FROM events r
                WHERE r.entity_type = '_internal'
                  AND r.action = 'dispatch_publish_replayed'
                  AND (r.payload->>'original_failure_event_id') = f.id::text
              )
            ORDER BY f.created_at ASC
            LIMIT :batch
            """
        )
        result = await session.execute(sql, {"batch": _BATCH_SIZE})
        return list(result.mappings())

    async def _replay_one(
        self, session: AsyncSession, marker_row: Any
    ) -> bool:
        """Re-publish the original event referenced by *marker_row*.

        Returns ``True`` iff the re-publish succeeded AND the
        ``dispatch_publish_replayed`` row was inserted. Any failure is
        logged + swallowed so other markers in the batch still get a
        chance; the marker stays unresolved and the next tick retries.
        """
        # Validate the marker payload through the registry — gives us a
        # typed handle to ``original_event_id`` / ``target`` and rejects
        # any corrupted row up front. ``UnknownEventTypeError`` is its
        # own except arm so a registry-vs-table mismatch surfaces with
        # a different log line than a payload-shape drift.
        try:
            marker = parse_payload(
                marker_row["entity_type"],
                marker_row["action"],
                marker_row["payload"],
            )
        except UnknownEventTypeError:
            logger.warning(
                "replay loop: marker %s has unknown event type %s.%s, "
                "skipping (registry drift?)",
                marker_row["id"],
                marker_row["entity_type"],
                marker_row["action"],
            )
            return False
        except ValidationError as exc:
            logger.warning(
                "replay loop: marker %s has unparseable payload, skipping: %s",
                marker_row["id"], exc,
            )
            return False
        assert isinstance(marker, DispatchPublishFailed)

        # v0 scope: only SNS-publish failures are healed via the events
        # publisher. SQS work-queue claim failures need a different
        # transport — the dispatcher's send_message path — that this
        # loop does not own. Log + skip; future work adds an SQS arm.
        if marker.target != "sns":
            logger.debug(
                "replay loop: marker %s has target=%s, not yet supported; "
                "leaving unresolved",
                marker_row["id"], marker.target,
            )
            return False

        original = await session.get(Event, marker.original_event_id)
        if original is None:
            logger.warning(
                "replay loop: marker %s references missing original event %s; "
                "skipping (will retry next tick in case of a race, but "
                "this likely indicates data corruption)",
                marker_row["id"], marker.original_event_id,
            )
            return False

        # Re-validate the original event's payload through the registry
        # so the publisher gets a typed payload (matches the happy-path
        # contract of ``EventPublisher.publish``).
        try:
            typed_payload = parse_payload(
                original.entity_type, original.action, original.payload
            )
        except (UnknownEventTypeError, ValidationError) as exc:
            logger.warning(
                "replay loop: original event %s payload failed validation, "
                "marker %s left unresolved: %s",
                original.id, marker_row["id"], exc,
            )
            return False

        try:
            await self.publisher.publish(original, typed_payload)
        except Exception as exc:
            logger.warning(
                "replay loop: publish for marker %s still failing, "
                "will retry next tick: %s",
                marker_row["id"], exc,
            )
            return False

        # Insert the resolution sibling. The marker stays as-is — the
        # event row is the source of truth and immutability matters more
        # than saving one row.
        replayed_payload = DispatchPublishReplayed(
            original_failure_event_id=_to_uuid(marker_row["id"]),
            original_event_id=marker.original_event_id,
            replayed_at=datetime.now(timezone.utc),
        )
        resolution = Event(
            entity_type=DispatchPublishReplayed.ENTITY_TYPE,
            action=DispatchPublishReplayed.ACTION,
            plan_id=original.plan_id,
            task_id=original.task_id,
            run_id=original.run_id,
            step_id=original.step_id,
            payload=replayed_payload.model_dump(mode="json"),
        )
        session.add(resolution)
        await session.flush()
        logger.info(
            "replay loop: healed marker %s (original event %s)",
            marker_row["id"], original.id,
        )
        return True


def _to_uuid(value: Any) -> uuid.UUID:
    """Coerce a row-mapping id (already a UUID via asyncpg) to a UUID
    instance — keeps the type signature clean when the driver hands us
    back a string or UUID interchangeably."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
