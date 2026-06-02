"""``/api/v1/escalations`` — operator-facing escalation incident surface (ADR-0062 Step 3).

The dashboard surface at ``/api/v1/dashboard/overview`` is page-shaped (it
returns the full operator-bucket payload). This router is CLI-shaped: each
endpoint is the data surface behind one ``treadmill escalations`` subcommand.

  * ``GET  /api/v1/escalations``                — list open incidents.
  * ``GET  /api/v1/escalations/stream``         — SSE feed of new opens +
                                                  closes + acks for ``tail``.
  * ``POST /api/v1/escalations/{task_id}/close`` — emit
                                                   ``escalation_closed`` with
                                                   ``close_reason='operator_close'``
                                                   via the shared
                                                   ``emit_operator_close``
                                                   helper.
  * ``POST /api/v1/escalations/{task_id}/ack``   — emit ``escalation_acknowledged``.
  * ``GET  /api/v1/escalations/report``         — MTTR aggregation over
                                                  the closed-incident
                                                  window, grouped by
                                                  ``reason`` / ``day`` /
                                                  ``task``.

The list query mirrors the shape of ``routers/dashboard/overview.py``
``_ESCALATIONS_SQL`` (the same dashboard predicate) but adds optional
``?reason=`` and ``?task=`` prefix filters so the CLI can scope to a
single class of incident or a specific task by prefix.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.coordination.escalation_close_sweep import emit_operator_close
from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher
from treadmill_api.eventbus import subscribe_local, unsubscribe_local
from treadmill_api.events.task import TaskEscalationAcknowledged


router = APIRouter(prefix="/api/v1/escalations", tags=["escalations"])

logger = logging.getLogger("treadmill.routers.escalations")


# ── Response shapes ──────────────────────────────────────────────────────────


class OpenEscalation(BaseModel):
    """One row in the list of open escalations.

    Field names match the dashboard's ``Escalation`` shape so a CLI / dashboard
    can interchange representations without re-mapping. ``opened_at`` is the
    server-side timestamp on the ``task.escalated_to_operator`` event row.
    """

    task_id: str
    repo: str
    title: str
    opened_at: datetime
    reason: str | None = None


class CloseResponse(BaseModel):
    task_id: str
    close_reason: Literal["operator_close"]
    mttr_seconds: int


class AckResponse(BaseModel):
    event_id: str
    task_id: str


class ReportBucket(BaseModel):
    """One row of the MTTR report — generic enough to cover every ``by`` value.

    ``key`` is the group-by value (a reason slug, an ISO date string, or a
    task id depending on ``by``). ``count`` is the number of closed
    incidents in the bucket; ``mttr_seconds_avg`` / ``mttr_seconds_p50`` /
    ``mttr_seconds_p95`` summarize the incident-duration distribution.
    """

    key: str
    count: int
    mttr_seconds_avg: int
    mttr_seconds_p50: int
    mttr_seconds_p95: int


class ReportResponse(BaseModel):
    since: datetime
    by: Literal["reason", "day", "task"]
    total: int
    buckets: list[ReportBucket]


# ── SQL ──────────────────────────────────────────────────────────────────────


# Open incidents — mirrors ``routers/dashboard/overview.py`` ``_ESCALATIONS_SQL``
# but adds the close-event filter (ADR-0062: an incident is OPEN iff its most
# recent ``escalated_to_operator`` has no later ``escalation_acknowledged``
# AND no later ``escalation_closed``). We add the close branch alongside the
# existing ack branch so the CLI's open-incidents view honors the full Step-2
# close-detection lifecycle.
_OPEN_SQL = """
WITH last_escalation AS (
    SELECT DISTINCT ON (task_id)
        task_id,
        created_at AS opened_at,
        payload->>'reason' AS reason
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalated_to_operator'
      AND task_id IS NOT NULL
    ORDER BY task_id, created_at DESC
),
last_ack AS (
    SELECT DISTINCT ON (task_id)
        task_id,
        created_at AS acked_at
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalation_acknowledged'
      AND task_id IS NOT NULL
    ORDER BY task_id, created_at DESC
),
last_close AS (
    SELECT DISTINCT ON (task_id)
        task_id,
        created_at AS closed_at
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalation_closed'
      AND task_id IS NOT NULL
    ORDER BY task_id, created_at DESC
)
SELECT
    le.task_id::text AS task_id,
    t.repo           AS repo,
    t.title          AS title,
    le.opened_at     AS opened_at,
    le.reason        AS reason
FROM last_escalation le
JOIN tasks t ON t.id = le.task_id
LEFT JOIN last_ack   la ON la.task_id = le.task_id
LEFT JOIN last_close lc ON lc.task_id = le.task_id
WHERE (la.acked_at IS NULL OR la.acked_at < le.opened_at)
  AND (lc.closed_at IS NULL OR lc.closed_at < le.opened_at)
ORDER BY le.opened_at DESC
"""


# Find the matching open-incident ``opened_at`` for a specific task. The
# operator-close + ack paths both need this to compute MTTR and to short-
# circuit when there's nothing open to close.
_OPEN_FOR_TASK_SQL = text("""
WITH last_escalation AS (
    SELECT created_at AS opened_at
    FROM events
    WHERE task_id = :task_id
      AND entity_type = 'task'
      AND action = 'escalated_to_operator'
    ORDER BY created_at DESC
    LIMIT 1
),
last_close AS (
    SELECT created_at AS closed_at
    FROM events
    WHERE task_id = :task_id
      AND entity_type = 'task'
      AND action = 'escalation_closed'
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT
    (SELECT opened_at FROM last_escalation) AS opened_at,
    (SELECT closed_at FROM last_close)      AS closed_at
""")


_TASK_EXISTS_SQL = text("SELECT 1 FROM tasks WHERE id = :task_id")


# MTTR report — pulls every ``escalation_closed`` event since ``:since`` and
# returns the per-event close_reason + mttr alongside enough columns to
# group by reason / day / task in Python. Aggregating in the application
# layer (vs. three separate GROUP BY variants in SQL) keeps the wire
# contract uniform and lets percentiles use a single sorted list per
# bucket — Postgres percentile_disc would work too but bloats the SQL
# without buying anything for the operator-scale data volume.
_CLOSED_EVENTS_SQL = text("""
SELECT
    e.task_id::text                       AS task_id,
    e.created_at                          AS closed_at,
    (e.payload->>'close_reason')          AS close_reason,
    (e.payload->>'mttr_seconds')::bigint  AS mttr_seconds
FROM events e
WHERE e.entity_type = 'task'
  AND e.action = 'escalation_closed'
  AND e.created_at >= :since
ORDER BY e.created_at ASC
""")


_INSERT_ACK_SQL = text("""
    INSERT INTO events (entity_type, action, task_id, payload)
    VALUES ('task', 'escalation_acknowledged', :task_id, '{}'::jsonb)
    RETURNING id
""")


# ── GET /api/v1/escalations ──────────────────────────────────────────────────


@router.get("", response_model=list[OpenEscalation])
async def list_escalations(
    session: Annotated[AsyncSession, Depends(get_session)],
    reason: Annotated[
        Literal["architect_cap", "stuck_task_sweep", "gate-broken"] | None,
        Query(description="Filter to escalations with this ``payload.reason``."),
    ] = None,
    task: Annotated[
        str | None,
        Query(description="Case-insensitive prefix match on task_id."),
    ] = None,
) -> list[OpenEscalation]:
    """List every open escalation incident.

    An incident is open iff the most recent ``task.escalated_to_operator``
    on the task has no later ``task.escalation_acknowledged`` and no
    later ``task.escalation_closed`` (matches the Step-2 sweep's open-set
    SQL plus the ack path so ack alone still drops a task off the list).

    Filtering happens in Python so the SQL remains a single hot path that
    every consumer (the CLI's ``list`` and ``tail``-bootstrap, the future
    operator dashboard's tile) can reuse without forking.
    """
    rows = (await session.execute(text(_OPEN_SQL))).mappings().all()
    out: list[OpenEscalation] = []
    for row in rows:
        if reason is not None and row["reason"] != reason:
            continue
        if task is not None and not row["task_id"].lower().startswith(task.lower()):
            continue
        out.append(
            OpenEscalation(
                task_id=row["task_id"],
                repo=row["repo"],
                title=row["title"],
                opened_at=row["opened_at"],
                reason=row["reason"],
            )
        )
    return out


# ── GET /api/v1/escalations/stream ────────────────────────────────────────────


# Event actions the stream surfaces. The CLI ``tail`` command needs to see
# every incident-lifecycle transition; everything else is noise.
_STREAM_ACTIONS = frozenset(
    {"escalated_to_operator", "escalation_acknowledged", "escalation_closed"},
)


def _sse_frame(data: dict[str, Any]) -> bytes:
    """Render one SSE ``data:`` frame. Wire format per the spec —
    ``data: <json>\\n\\n``. We keep frames tiny so a slow client doesn't
    head-of-line block the in-process publisher queue."""
    return f"data: {json.dumps(data)}\n\n".encode("utf-8")


def _comment(text_str: str) -> bytes:
    """Render an SSE comment line (starts with ``:``). Used for the
    initial open marker + the keepalive ticks; not surfaced to consumers
    as ``message`` events."""
    return f": {text_str}\n\n".encode("utf-8")


async def _escalation_event_stream(
    request: Request, heartbeat_interval: float,
) -> AsyncIterator[bytes]:
    """Generator backing the SSE response.

    Subscribes to the in-process publisher fan-out
    (``eventbus.subscribe_local``) the same way the dashboard's
    ``ws.py`` does, but filters to escalation-lifecycle actions and
    formats each frame as SSE rather than a WS JSON message. Heartbeat
    comments keep proxies + load-balancers from idling the connection.
    """
    queue = subscribe_local()
    try:
        # Initial connect marker — comments are ignored by the
        # EventSource API but visible to a raw ``curl`` tail.
        yield _comment("connected")
        while True:
            if await request.is_disconnected():
                return
            try:
                record = await asyncio.wait_for(
                    queue.get(), timeout=heartbeat_interval,
                )
            except asyncio.TimeoutError:
                yield _comment("ping")
                continue
            action = record.get("action")
            entity_type = record.get("entity_type")
            if entity_type != "task" or action not in _STREAM_ACTIONS:
                continue
            yield _sse_frame(
                {
                    "id": record.get("event_id"),
                    "entity_type": entity_type,
                    "action": action,
                    "task_id": record.get("task_id"),
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
    finally:
        unsubscribe_local(queue)


@router.get("/stream")
async def stream_escalations(
    request: Request,
    heartbeat_interval: Annotated[
        float,
        Query(
            gt=0,
            description=(
                "Seconds between SSE keepalive comments. Defaults to 15."
            ),
        ),
    ] = 15.0,
) -> StreamingResponse:
    """SSE feed of escalation-lifecycle events for the CLI ``tail`` view.

    The stream emits one ``data:``-prefixed JSON frame per new
    ``task.escalated_to_operator`` / ``task.escalation_acknowledged`` /
    ``task.escalation_closed`` event the publisher fans out in-process.
    ``: ping`` comments fire every ``heartbeat_interval`` seconds so
    proxies don't idle the socket and the CLI can detect dropped
    connections faster than TCP keepalive.

    The endpoint never replays history — pair with ``GET /api/v1/escalations``
    for the bootstrap snapshot, then connect here for the deltas (the
    standard "snapshot + delta" pattern).
    """
    return StreamingResponse(
        _escalation_event_stream(request, heartbeat_interval),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


# ── POST /api/v1/escalations/{task_id}/close ─────────────────────────────────


@router.post(
    "/{task_id}/close",
    response_model=CloseResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def close_escalation(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> CloseResponse:
    """Emit ``task.escalation_closed`` with ``close_reason='operator_close'``.

    Routes through the shared ``emit_operator_close`` helper so the close
    emission code path + MTTR computation are single-sourced with the
    Step-2 sweep. Returns 404 when the task does not exist, 409 when the
    task has no open incident to close.
    """
    exists = (
        await session.execute(_TASK_EXISTS_SQL, {"task_id": task_id})
    ).first()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )

    row = (
        await session.execute(_OPEN_FOR_TASK_SQL, {"task_id": task_id})
    ).one()
    opened_at = row.opened_at
    closed_at = row.closed_at
    if opened_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task has no open escalation",
        )
    if closed_at is not None and closed_at >= opened_at:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task's most recent escalation is already closed",
        )

    now = datetime.now(timezone.utc)
    await emit_operator_close(
        session, dispatcher, task_id=task_id, opened_at=opened_at, now=now,
    )
    await session.commit()
    mttr_seconds = int((now - opened_at).total_seconds())
    return CloseResponse(
        task_id=str(task_id),
        close_reason="operator_close",
        mttr_seconds=mttr_seconds,
    )


# ── POST /api/v1/escalations/{task_id}/ack ───────────────────────────────────


@router.post(
    "/{task_id}/ack",
    response_model=AckResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ack_escalation(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> AckResponse:
    """Emit ``task.escalation_acknowledged`` for ``task_id``.

    Mirrors the dashboard ack endpoint at
    ``routers/dashboard/ack_escalation.py`` but routes through the
    dispatcher's ``persist_and_publish`` seam so the typed event lands on
    the publisher fan-out (the SSE ``/stream`` endpoint observes the ack
    that way). 404 when the task does not exist, 409 when no open
    incident exists to acknowledge.
    """
    exists = (
        await session.execute(_TASK_EXISTS_SQL, {"task_id": task_id})
    ).first()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )

    row = (
        await session.execute(_OPEN_FOR_TASK_SQL, {"task_id": task_id})
    ).one()
    if row.opened_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task is not currently escalated",
        )

    event = await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="escalation_acknowledged",
        payload=TaskEscalationAcknowledged(),
        task_id=task_id,
    )
    await session.commit()
    return AckResponse(event_id=str(event.id), task_id=str(task_id))


# ── GET /api/v1/escalations/report ───────────────────────────────────────────


def _percentile(sorted_values: list[int], p: float) -> int:
    """Nearest-rank percentile on a pre-sorted list. Returns 0 for an
    empty input (the caller filters empty buckets out before calling)."""
    if not sorted_values:
        return 0
    n = len(sorted_values)
    # Nearest-rank, 1-indexed: ceil(p/100 * n).
    rank = max(1, int(-(-p * n // 100)))
    return int(sorted_values[min(rank, n) - 1])


@router.get("/report", response_model=ReportResponse)
async def report_escalations(
    session: Annotated[AsyncSession, Depends(get_session)],
    since: Annotated[
        datetime | None,
        Query(description="ISO timestamp; defaults to 7 days ago."),
    ] = None,
    by: Annotated[
        Literal["reason", "day", "task"],
        Query(description="Group-by dimension for the bucket rows."),
    ] = "reason",
) -> ReportResponse:
    """MTTR aggregation across all ``task.escalation_closed`` events.

    Buckets every close event since ``:since`` by ``reason`` / ``day``
    (UTC date string) / ``task`` and reports ``count`` + ``mttr_seconds_avg`` +
    p50 + p95 per bucket. Buckets are sorted by ``count`` desc so the
    busiest reason / day / task lands at the top — the same shape the
    CLI ``report`` table renders. Default window is 7 days; pass
    ``?since=`` to widen / narrow.
    """
    if since is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)
    else:
        since_dt = since if since.tzinfo else since.replace(tzinfo=timezone.utc)

    rows = (
        await session.execute(_CLOSED_EVENTS_SQL, {"since": since_dt})
    ).mappings().all()

    # Bucket the rows.
    buckets: dict[str, list[int]] = {}
    for row in rows:
        if by == "reason":
            key = row["close_reason"] or "unknown"
        elif by == "task":
            key = row["task_id"] or "unknown"
        else:  # by == "day"
            closed_at: datetime = row["closed_at"]
            key = closed_at.astimezone(timezone.utc).date().isoformat()
        buckets.setdefault(key, []).append(int(row["mttr_seconds"] or 0))

    out: list[ReportBucket] = []
    for key, values in buckets.items():
        values_sorted = sorted(values)
        avg = sum(values_sorted) // len(values_sorted)
        out.append(
            ReportBucket(
                key=key,
                count=len(values_sorted),
                mttr_seconds_avg=avg,
                mttr_seconds_p50=_percentile(values_sorted, 50),
                mttr_seconds_p95=_percentile(values_sorted, 95),
            )
        )
    out.sort(key=lambda b: (-b.count, b.key))

    return ReportResponse(
        since=since_dt,
        by=by,
        total=len(rows),
        buckets=out,
    )
