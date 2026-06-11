"""``WS /api/v1/dashboard/ws/events`` — live event feed for the operator dashboard.

The dashboard's ``<ConnectionAffordance>`` chip (DESIGN.md rule #8) only
tells the truth if the page is actually subscribed to a live signal.
This sibling exposes that signal: a WebSocket that relays each event row
the API publishes (via ``treadmill_api.eventbus``) the instant the
publisher fans it out in-process. No Postgres tail, no polling — the
publisher is the seam.

Wire shape — three message ``type``s the client must handle:

  * ``hello``     — sent once on connect; carries server ``ts``.
  * ``event``     — one per published event row. Carries the small set
                    of fields the dashboard cares about (``entity_type``,
                    ``action``, ``task_id``, ``plan_id``, ``ts``, ``id``).
  * ``heartbeat`` — every ``heartbeat_interval`` seconds. Lets a client
                    detect a dead socket faster than TCP keepalive does.

Backpressure: ``send_json`` is wrapped in ``asyncio.wait_for`` with a
1 s budget. A blocked send means the client (or the network in front of
it) can't keep up; we'd rather drop and let the client reconnect than
let one slow consumer wedge the publish loop's in-process queue.

The heartbeat interval is configurable so tests don't have to wait 25 s
to observe one. Production callers (the auto-discovery loop) get the
default.

Optional ``?created_by=<label>`` filter (ADR-0068): when set, only event
frames whose owning plan or task matches the label are forwarded.
Ownership is resolved via ``plans.created_by`` (preferred) or
``tasks.created_by``; ownerless events are dropped on filtered
connections. Heartbeat and hello frames bypass the filter entirely.
Positive resolutions are cached per-connection; negative resolutions
expire after ``_NEGATIVE_TTL_S`` because the in-process broadcast runs
inside the emitter's still-open transaction, so a lookup can race the
commit (see the cache comment in ``events_socket``).

``?coordinator_label=<label>`` additionally matches on the event
payload's own ``coordinator_label`` field before any DB lookup —
``plan.submitted`` carries it, and the payload path is the only one
that cannot lose the race against the submitting transaction's commit.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from treadmill_api.eventbus import subscribe_local, unsubscribe_local

logger = logging.getLogger("treadmill.dashboard.ws")


router = APIRouter()


# Send-side budget for a single ``send_json``. A client that can't drain
# a frame inside this window is dropped — reconnect is cheaper than
# stalling the in-process publish queue.
_SEND_TIMEOUT_S = 1.0

# Default heartbeat cadence. Spec'd at 25 s so a stalled NAT / proxy
# trips inside most idle-connection windows (60 s on common load
# balancers).
_DEFAULT_HEARTBEAT_S = 25.0

# How long a NEGATIVE owner-lookup result (no row resolved) stays cached.
# Events are broadcast in-process from inside the emitter's still-open
# transaction, so a lookup can race the commit and legitimately miss a row
# that exists milliseconds later — a permanent negative cache would blind
# the socket to that plan/task for its whole lifetime. Positive results
# still cache forever. Tests shrink this to 0 to exercise re-resolution.
_NEGATIVE_TTL_S = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_frame(record: dict[str, Any]) -> dict[str, Any]:
    """Project a publisher record (see ``eventbus._build_record``) into
    the small frame the dashboard consumes. We deliberately keep this
    narrow — the right-rail event tail re-fetches the typed payload via
    the existing ``/overview`` endpoint when it needs the details."""
    return {
        "type": "event",
        "id": record.get("event_id"),
        "entity_type": record.get("entity_type"),
        "action": record.get("action"),
        "task_id": record.get("task_id"),
        "plan_id": record.get("plan_id"),
        "ts": _now_iso(),
    }


async def _safe_send(websocket: WebSocket, frame: dict[str, Any]) -> bool:
    """Send a frame with a bounded budget. Returns ``False`` if the send
    timed out (caller closes the socket) or the peer disconnected."""
    try:
        await asyncio.wait_for(
            websocket.send_json(frame), timeout=_SEND_TIMEOUT_S,
        )
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "dashboard WS send exceeded %.1fs budget; dropping client",
            _SEND_TIMEOUT_S,
        )
        return False
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError covers "Cannot call send once the connection
        # has been closed" from Starlette.
        return False


async def _lookup_created_by(
    plan_id: str | None,
    task_id: str | None,
    session_factory: Any = None,
) -> str | None:
    """Look up the created_by label for an event record.

    Prefers ``plan_id`` → ``plans.created_by``; falls back to
    ``task_id`` → ``tasks.created_by``. Returns ``None`` when neither
    resolves or when no session factory is available (e.g. no
    ``DATABASE_URL`` at startup).

    Tests monkeypatch this function with a stub mapping to avoid DB
    access — the caller in ``events_socket`` always calls the module-
    level name so the patch is effective.
    """
    if plan_id is None and task_id is None:
        return None
    if session_factory is None:
        return None
    async with session_factory() as session:
        if plan_id is not None:
            result = await session.execute(
                text("SELECT created_by FROM plans WHERE id = :id"),
                {"id": uuid.UUID(plan_id)},
            )
            row = result.fetchone()
            return row[0] if row else None
        result = await session.execute(
            text("SELECT created_by FROM tasks WHERE id = :id"),
            {"id": uuid.UUID(task_id)},
        )
        row = result.fetchone()
        return row[0] if row else None


async def _lookup_coordinator_label(
    plan_id: str | None,
    session_factory: Any = None,
) -> str | None:
    """Look up the coordinator_label for the repo owning ``plan_id``.

    Resolves via ``plans JOIN team_configs ON team_configs.repo =
    plans.repo``. Returns ``None`` when the plan doesn't exist, when the
    repo has no ``team_configs`` row (no coordinator registered), or
    when no session factory is available.

    This is the ADR-0085+0086 fix for the in-session plan-pickup gap:
    new plans are submitted with ``created_by=<orchestrator-label>`` (e.g.
    ``treadmill-alan``), so a ``coordinator-medicoder`` socket
    subscribing only on ``created_by`` and ``plan_ids`` never sees its
    own ``plan.submitted`` because (a) the plan is new — not in
    ``plan_ids`` yet, and (b) ``created_by`` doesn't match. This helper
    backs the third filter branch (``coordinator_label``) that closes
    the gap by resolving plan → repo → coordinator_label per event.

    Tests monkeypatch this function with a stub mapping to avoid DB
    access — the caller in ``events_socket`` always calls the module-
    level name so the patch is effective.
    """
    if plan_id is None or session_factory is None:
        return None
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT tc.coordinator_label "
                "FROM plans p "
                "JOIN team_configs tc ON tc.repo = p.repo "
                "WHERE p.id = :id"
            ),
            {"id": uuid.UUID(plan_id)},
        )
        row = result.fetchone()
        return row[0] if row else None


@router.websocket("/ws/events")
async def events_socket(
    websocket: WebSocket,
    heartbeat_interval: float = Query(
        _DEFAULT_HEARTBEAT_S,
        gt=0,
        description=(
            "Seconds between heartbeat frames. Defaults to 25; tests can "
            "shrink it to observe the cadence without long waits."
        ),
    ),
    created_by: str | None = Query(
        None,
        max_length=255,
        description=(
            "When set, only event frames whose owning plan or task "
            "carries this created_by label are forwarded. Heartbeat and "
            "hello frames are unaffected. Events with no resolvable "
            "owner are dropped on filtered connections."
        ),
    ),
    plan_ids: str | None = Query(
        None,
        max_length=4096,
        description=(
            "Comma-separated list of plan UUIDs the subscriber owns "
            "(ADR-0084 coordinator subscription). When set, events whose "
            "``plan_id`` is in the list are forwarded, regardless of "
            "``created_by``. Coordinators use this to receive plan-scoped "
            "events for plans whose tasks were dispatched by other "
            "workers. ``created_by`` and ``plan_ids`` compose by OR: an "
            "event is forwarded if EITHER filter matches. Ownerless "
            "events are still dropped on any filtered connection. "
            "Malformed UUIDs are silently skipped."
        ),
    ),
    coordinator_label: str | None = Query(
        None,
        max_length=255,
        description=(
            "When set, events whose plan belongs to this coordinator are "
            "also forwarded. Resolved via plans JOIN team_configs ON "
            "team_configs.repo = plans.repo. Composes by OR with "
            "created_by and plan_ids: an event is forwarded if ANY filter "
            "matches. Coordinators use this to receive plan.submitted "
            "events for newly-submitted plans not yet in plan_ids and "
            "whose created_by is the submitting orchestrator (ADR-0085+0086 "
            "in-session pickup gap)."
        ),
    ),
) -> None:
    """Stream live event records to a dashboard client.

    The handler runs three concurrent reads and races them so the loop
    exits cleanly on any of:

      * a published event arriving on the in-process queue,
      * the heartbeat timer firing,
      * the client closing the socket (``receive`` returns a disconnect).
    """
    await websocket.accept()
    # Subscribe BEFORE the hello hits the wire so events published in
    # the gap between the client observing hello and the loop starting
    # don't go missing. The cost is one queue allocation per failed
    # hello send; cleaned up in the ``finally`` either way.
    queue = subscribe_local()
    if not await _safe_send(
        websocket, {"type": "hello", "ts": _now_iso()},
    ):
        # Hello couldn't even land — close immediately.
        unsubscribe_local(queue)
        await _safe_close(websocket)
        return

    # Build a session factory for owner lookups (used only when
    # ``created_by`` is active). Falls back to None when the engine
    # wasn't wired (no DATABASE_URL), in which case ``_lookup_created_by``
    # returns None and every event is ownerless-dropped on a filtered
    # connection.
    _engine = getattr(websocket.app.state, "engine", None)
    _session_factory = (
        async_sessionmaker(_engine, expire_on_commit=False)
        if _engine is not None
        else None
    )
    # Per-connection owner cache: ``"plan:<id>"`` / ``"task:<id>"`` → label.
    # Positive results never evict (connections are short-lived relative
    # to plan/task lifetimes). Negative results (``None``) expire after
    # ``_NEGATIVE_TTL_S``: events are broadcast in-process from INSIDE the
    # emitter's still-open transaction (``persist_and_publish`` runs before
    # the router commits), so a lookup racing that commit legitimately sees
    # no row yet — caching that ``None`` forever would permanently blind
    # the socket to the plan/task (the plan.submitted pickup loss,
    # task 9b7c1286).
    _owner_cache: dict[str, str | None] = {}
    _negative_deadline: dict[str, float] = {}

    async def _resolve_cached(key: str, lookup: Any) -> str | None:
        """Owner-cache wrapper around an async ``lookup()`` thunk.

        Lookup exceptions propagate to the caller (which drops the event
        and keeps the socket alive) without poisoning the cache.
        """
        if key in _owner_cache:
            cached = _owner_cache[key]
            if cached is not None:
                return cached
            if time.monotonic() < _negative_deadline.get(key, 0.0):
                return None
            # Negative entry expired — re-resolve below.
        value = await lookup()
        _owner_cache[key] = value
        if value is None:
            _negative_deadline[key] = time.monotonic() + _NEGATIVE_TTL_S
        else:
            _negative_deadline.pop(key, None)
        return value

    # Coordinator plan-id subscription (ADR-0084). Parsed once at connect
    # time; subsequent reconnects re-parse from the new query string.
    # Malformed entries are dropped silently — the empty set means the
    # plan-ids filter is inactive (created_by alone governs).
    _plan_id_set: set[str] = set()
    if plan_ids:
        for raw in plan_ids.split(","):
            candidate = raw.strip()
            if not candidate:
                continue
            try:
                # Normalise to canonical lower-case hyphenated form so
                # an event's ``plan_id`` (already a str on the wire)
                # compares against the same canonical form regardless of
                # how the operator wrote it on the query string.
                _plan_id_set.add(str(uuid.UUID(candidate)))
            except ValueError:
                logger.warning(
                    "plan_ids: dropping malformed UUID %r", candidate,
                )
    _filter_active = (
        created_by is not None
        or bool(_plan_id_set)
        or coordinator_label is not None
    )

    # ``receive`` doubles as a disconnect detector — the dashboard never
    # sends client→server frames, so any completion of this task is our
    # cue to tear down. Starlette returns the disconnect message as a
    # dict (rather than raising) the first time; subsequent calls would
    # raise ``RuntimeError``. Either way the loop should stop, so we
    # never re-arm this task.
    receive_task = asyncio.create_task(websocket.receive())
    event_task = asyncio.create_task(queue.get())
    heartbeat_task = asyncio.create_task(asyncio.sleep(heartbeat_interval))

    try:
        while True:
            done, _pending = await asyncio.wait(
                {receive_task, event_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if receive_task in done:
                # Drain the result/exception so asyncio doesn't warn about
                # an unawaited exception, then stop the loop.
                receive_task.exception()
                break

            if event_task in done:
                record = event_task.result()
                event_task = asyncio.create_task(queue.get())

                if _filter_active:
                    plan_id = record.get("plan_id")
                    task_id = record.get("task_id")
                    if not plan_id and not task_id:
                        # Ownerless event — drop on filtered connections.
                        continue

                    # Three-way OR filter (ADR-0084 + ADR-0085+0086):
                    # forward if ANY of plan_ids / coordinator_label /
                    # created_by matches. Each branch is independent and
                    # we early-set ``matched`` to skip work on cheaper
                    # hits — plan_ids is the in-memory hot path; the
                    # two label branches each cost one DB lookup
                    # (cached per (plan_id|task_id) for the lifetime of
                    # the socket).
                    matched = False

                    # 1. plan_ids fast path. In-memory set lookup; no DB.
                    if (
                        _plan_id_set
                        and plan_id
                        and str(plan_id) in _plan_id_set
                    ):
                        matched = True

                    # 2. coordinator_label. ADR-0085+0086 in-session
                    # plan-pickup gap: new plans carry
                    # created_by=<submitting-orchestrator>, so a
                    # coordinator subscribed only on created_by + plan_ids
                    # never sees plan.submitted for its own plans.
                    #
                    # 2a. Payload fast path — ``plan.submitted`` carries
                    # ``coordinator_label`` in its payload (the emitter
                    # resolved team_configs inside the submitting
                    # transaction), so match on it directly: no DB, and —
                    # the correctness half — no read racing the emitter's
                    # still-open transaction. ``plan.submitted`` is
                    # broadcast BEFORE ``POST /plans`` commits, so a DB
                    # lookup from this (separate) session cannot see the
                    # plan row yet and would drop the one event this
                    # filter branch exists to deliver (task 9b7c1286).
                    if not matched and coordinator_label is not None:
                        _payload = record.get("payload")
                        if (
                            isinstance(_payload, dict)
                            and _payload.get("coordinator_label")
                            == coordinator_label
                        ):
                            matched = True

                    # 2b. DB path for events that don't carry the label:
                    # resolves plan → repo → coordinator_label per event.
                    if (
                        not matched
                        and coordinator_label is not None
                        and plan_id is not None
                    ):
                        try:
                            resolved = await _resolve_cached(
                                f"coordinator:{plan_id}",
                                lambda pid=plan_id: _lookup_coordinator_label(
                                    pid, _session_factory
                                ),
                            )
                        except Exception:
                            logger.exception(
                                "coordinator_label lookup failed "
                                "for plan %s; dropping event",
                                plan_id,
                            )
                            continue  # drop; socket stays alive
                        if resolved == coordinator_label:
                            matched = True

                    # 3. created_by. Resolves plan → plans.created_by or
                    # task → tasks.created_by per ADR-0084's original
                    # filter. Cached per (plan|task) id so a flood of
                    # events on the same plan only hits the DB once.
                    if not matched and created_by is not None:
                        if plan_id:
                            cache_key = f"plan:{plan_id}"
                        elif task_id:
                            cache_key = f"task:{task_id}"
                        else:
                            continue  # defensive — already guarded above
                        try:
                            resolved = await _resolve_cached(
                                cache_key,
                                lambda p=plan_id, t=task_id: _lookup_created_by(
                                    p, t, _session_factory
                                ),
                            )
                        except Exception:
                            logger.exception(
                                "created_by lookup failed for %s; "
                                "dropping event",
                                cache_key,
                            )
                            continue  # drop; socket stays alive
                        if resolved == created_by:
                            matched = True

                    if not matched:
                        continue  # no filter matched → drop

                if not await _safe_send(websocket, _event_frame(record)):
                    break

            if heartbeat_task in done:
                heartbeat_task = asyncio.create_task(
                    asyncio.sleep(heartbeat_interval),
                )
                if not await _safe_send(
                    websocket,
                    {"type": "heartbeat", "ts": _now_iso()},
                ):
                    break
    except Exception:
        # Belt-and-braces: a bug in the loop must never propagate into
        # the router runtime. Log and close so the socket is closed
        # cleanly on the way out.
        logger.exception("dashboard WS loop crashed; closing socket")
    finally:
        unsubscribe_local(queue)
        for task in (receive_task, event_task, heartbeat_task):
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    # Cancellation / disconnect noise during teardown is
                    # expected; swallow so the socket close still runs.
                    pass
        await _safe_close(websocket)


async def _safe_close(websocket: WebSocket) -> None:
    """Best-effort close. The peer may have already torn the connection
    down (``RuntimeError`` from Starlette in that case); either way the
    socket is gone by the time we return."""
    try:
        await websocket.close()
    except (RuntimeError, WebSocketDisconnect):
        pass
