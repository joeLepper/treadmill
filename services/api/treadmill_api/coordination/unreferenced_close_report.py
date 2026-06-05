"""Unreferenced escalation close report sweep.

Fires weekly (Mondays 09:00 UTC) to sweep the past 7 days of
``escalation_closed`` events with ``expected_followup`` null or empty.
Groups by repo and emits one ``system.unreferenced_closes_report`` event
per repo. The NotificationFanout (ADR-0062) is the primary consumer,
emitting operator alerts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.events.system import UnreferencedClose, UnreferencedClosesReport

logger = logging.getLogger("treadmill.coordination.unreferenced_close_report")


UNREFERENCED_CLOSE_REPORT_WORKFLOW_ID = "wf-unreferenced-close-report"
"""Schedule's ``workflow_id`` value. ``handle_scheduled_tick`` intercepts
this slug and runs the weekly report sweep instead of looking up a
``WorkflowVersion`` (there is none — this bot is a query, not a role)."""


_UNREFERENCED_CLOSES_SQL = text("""
    SELECT
        t.repo,
        e.task_id::text AS task_id,
        e.payload->>'close_reason' AS close_reason,
        (e.payload->>'mttr_seconds')::bigint AS mttr_seconds,
        e.created_at::text AS closed_at
    FROM events e
    JOIN tasks t ON t.id = e.task_id
    WHERE e.entity_type = 'task'
      AND e.action = 'escalation_closed'
      AND e.created_at >= :window_start
      AND e.created_at < :window_end
      AND (
        e.payload->>'expected_followup' IS NULL
        OR e.payload->>'expected_followup' = ''
      )
    ORDER BY t.repo, e.created_at ASC
""")
"""Find all unreferenced escalation closes in the past 7 days, grouped by
repo. A close is unreferenced if expected_followup is null or empty string."""


async def run_unreferenced_close_report_sweep(
    session: AsyncSession,
    dispatcher: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Sweep past 7 days of unreferenced closes and emit reports per repo.

    Returns the count of report events emitted. ``now`` is overridable
    for tests; production callers pass ``None`` and the sweep clocks itself.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    window_start = moment - timedelta(days=7)

    result = await session.execute(
        _UNREFERENCED_CLOSES_SQL,
        {"window_start": window_start, "window_end": moment},
    )
    rows = list(result)

    if not rows:
        logger.debug(
            "unreferenced-close-report sweep: no unreferenced closes in past 7 days at %s",
            moment.isoformat(),
        )
        return 0

    # Group by repo
    by_repo: dict[str, list[UnreferencedClose]] = {}
    for row in rows:
        repo = row.repo
        close = UnreferencedClose(
            task_id=row.task_id,
            close_reason=row.close_reason or "unknown",
            mttr_seconds=int(row.mttr_seconds or 0),
            closed_at=row.closed_at,
        )
        by_repo.setdefault(repo, []).append(close)

    # Emit one report event per repo
    emitted = 0
    for repo, closes in sorted(by_repo.items()):
        if dispatcher is None:
            continue
        try:
            await dispatcher.persist_and_publish(
                session,
                entity_type="system",
                action="unreferenced_closes_report",
                payload=UnreferencedClosesReport(
                    repo=repo,
                    closes=closes,
                    window_end=moment.isoformat(),
                ),
            )
            emitted += 1
        except Exception:
            logger.exception(
                "unreferenced-close-report sweep: failed to emit report for repo %s; "
                "next sweep tick will retry",
                repo,
            )

    logger.info(
        "unreferenced-close-report sweep: emitted %d report(s) for %d repo(s)",
        emitted, len(by_repo),
    )
    return emitted
