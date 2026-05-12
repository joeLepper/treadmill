"""Cache-then-heal pending-events buffering per ADR-0007.

When a GitHub webhook arrives before its task_prs bridge row exists (a
race that happens when ``pr_opened`` arrives faster than the worker can
report PR creation), we still persist the Event row with ``task_id =
NULL`` AND buffer the event in Redis. When the bridge row eventually
appears, the consumer that creates it calls ``drain_pending_events`` to
update the buffered events with the resolved ``task_id`` and re-publish
them on the bus so consumers see the resolved form.

Buffer key:  ``pr:{repo-lower}:{pr_number}:pending_events`` (Redis list)
Buffer TTL:  48 hours (matches bunkhouse).

The drain function is a utility — it doesn't fire automatically. The
caller that creates a task_prs row is responsible for invoking it. As of
v0 there is no such caller in the API; the drain ships ready for Week 2's
worker-completion path to use it.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.events import EventPayload, parse_payload
from treadmill_api.models import Event

logger = logging.getLogger("treadmill.pending_events")

PENDING_TTL_SECONDS = 48 * 3600


def buffer_key(repo: str, pr_number: int) -> str:
    """Redis list key for buffered events on a (repo, pr_number) pair.

    Repo is lowercased to match the case-insensitive lookup we use for
    task_prs (per the bunkhouse pattern).
    """
    return f"pr:{repo.lower()}:{pr_number}:pending_events"


async def buffer_pending_event(
    redis_client: Any,
    repo: str,
    pr_number: int,
    event_id: uuid.UUID,
) -> None:
    """Append an Event id to the pending-events buffer for replay later.

    We store only the event_id; the full payload is in the events table.
    On drain, we re-fetch the row, update its task_id, and re-publish.
    """
    key = buffer_key(repo, pr_number)
    record = json.dumps(
        {
            "event_id": str(event_id),
            "buffered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    await redis_client.rpush(key, record)
    await redis_client.expire(key, PENDING_TTL_SECONDS)
    logger.info(
        "buffered pending event_id=%s for repo=%s pr=%d (key=%s)",
        event_id, repo, pr_number, key,
    )


async def drain_pending_events(
    redis_client: Any,
    session: AsyncSession,
    publisher: Any,
    repo: str,
    pr_number: int,
    task_id: uuid.UUID,
) -> int:
    """Drain buffered events for (repo, pr_number) and resolve them.

    For each buffered event_id:
      1. Fetch the Event row.
      2. Update its task_id from NULL to the resolved value.
      3. Re-publish on the bus so consumers see the resolved form.

    Returns the count of events drained. Idempotent: an empty buffer
    returns 0 without raising.

    Implementation note: the events.task_id update is the one mutable-
    column write we accept here, in line with the single-writer
    projection pattern from ADR-0011 — the drain function is the only
    writer for this column.
    """
    key = buffer_key(repo, pr_number)
    drained = 0
    while True:
        raw = await redis_client.lpop(key)
        if raw is None:
            break
        try:
            record = json.loads(raw)
            event_id = uuid.UUID(record["event_id"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(
                "skipping malformed pending-event entry for key=%s: %s", key, exc
            )
            continue

        event = await session.get(Event, event_id)
        if event is None:
            logger.warning("drain: event_id=%s not found; skipping", event_id)
            continue

        if event.task_id is not None:
            # Already resolved (e.g. by a concurrent drainer); skip.
            logger.debug(
                "drain: event_id=%s already has task_id=%s; skipping",
                event_id, event.task_id,
            )
            continue

        event.task_id = task_id
        await session.flush()

        # Re-publish on the bus so consumers see the resolved form.
        try:
            typed = parse_payload(event.entity_type, event.action, event.payload)
            await publisher.publish(event, typed)
        except Exception:
            logger.exception(
                "drain: republish failed for event_id=%s; row is updated",
                event_id,
            )

        drained += 1

    if drained:
        await session.commit()
        logger.info(
            "drained %d pending event(s) for repo=%s pr=%d task_id=%s",
            drained, repo, pr_number, task_id,
        )
    return drained


async def pending_event_count(redis_client: Any, repo: str, pr_number: int) -> int:
    """Inspect the buffer length without draining (used by tests + status)."""
    return await redis_client.llen(buffer_key(repo, pr_number))
