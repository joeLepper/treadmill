"""Async accessor for the central event log — ADR-0093.

Append-only log: the outbox pump writes via ``append``; per-host consumers
read via ``read_for_labels``. Deduplication is not done here — it is the
consumer's responsibility via DedupBackend (claim→handle→commit cycle).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models.event_log import EventLog


class EventLogStore:
    """Async accessor for the ``event_log`` table."""

    async def append(self, session: AsyncSession, message: dict) -> EventLog:
        """Append a message to the event log. Returns the row with offset set.

        The caller owns the session transaction. Call ``await session.flush()``
        (or commit) before reading the Postgres-assigned offset from the
        returned row.
        """
        row = EventLog(
            dedup_key=message["dedupKey"],
            ordering_key=message["ordering_key"],
            event_type=message["event_type"],
            payload=message["payload"],
        )
        session.add(row)
        await session.flush()
        return row

    async def read_for_labels(
        self,
        session: AsyncSession,
        labels: list[str],
        after_offset: int = 0,
        limit: int = 100,
    ) -> list[EventLog]:
        """Return rows addressed to ``labels`` in append order after ``after_offset``.

        Filters: ``ordering_key IN (labels) AND offset > after_offset``.
        Returns an empty list immediately when ``labels`` is empty.
        """
        if not labels:
            return []
        result = await session.scalars(
            sa.select(EventLog)
            .where(EventLog.ordering_key.in_(labels))
            .where(EventLog.offset > after_offset)
            .order_by(EventLog.offset)
            .limit(limit)
        )
        return list(result)
