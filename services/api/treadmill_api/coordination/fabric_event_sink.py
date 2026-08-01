"""exec_otp fabric event push sink (ADR-0087 fabric migration).

This module owns the PUSH transport that feeds the exec_otp fabric's
``Treadmill-events`` connector. Historically the fabric held a WebSocket
open against the dashboard event feed (``routers/dashboard/ws.py``) and
filtered on ``?coordinator_label=<label>`` to receive the events for its
coordinator. Joe vetoed polling and the fabric migration replaces the
held socket with a server-side push: whenever Treadmill fans an event to
a coordinator in-process, we ALSO POST it to the fabric's HTTP ingress,
which routes by ``coordinator_label`` into that coordinator's fabric
``:global`` inbox.

Why subscribe in-process (mirroring ``NotificationFanout``)?
------------------------------------------------------------

The eventbus already fans every published record through
``subscribe_local`` / ``unsubscribe_local`` *before* the SNS hop (the
same seam the dashboard WebSocket rides, ADR-0056). This sink is one
more local subscriber. The trade-off — fan-out is per-API-process, not
durable across a restart — matches the notification fan-out's: a push
lost on restart is acceptable (the fabric reconciles from the events
table / dashboard on reconnect); correctness of the event pipeline must
never depend on the fabric being reachable.

Additive, dark-by-default
--------------------------

The dashboard WebSocket fan-out is left entirely in place — a
non-migrated coordinator still receives its events over the WS. The push
is purely additive. And it ships dark: with ``FABRIC_INGRESS_URL`` unset
the sink's ``start()`` is a no-op (no subscription, no task), so nothing
is pushed until the fabric's ingress URL is configured.

Wire shape (the contract Ernie's ingress parses)
------------------------------------------------

For each event that resolves to a coordinator, we POST JSON::

    {
      "coordinator_label": "<label>",   # ingress routes on this
      "event_type": "<entity_type>.<action>",  # e.g. "plan.submitted"
      "payload": { ... },               # the event's encoded payload dict
      "ts": "<iso8601-utc>"             # push time, UTC, offset-aware
    }

``coordinator_label`` resolution mirrors the WebSocket's
``?coordinator_label`` filter (``routers/dashboard/ws.py``):

  1. ``plan.submitted`` carries ``coordinator_label`` in its payload (the
     emitter resolved ``team_configs`` inside the submitting
     transaction). We read it directly — this is the one event published
     BEFORE its emitter commits, so a DB lookup from this separate
     session would race the commit and miss the row. Gated to
     ``plan.submitted`` exactly like the WS payload fast path: the
     manual-event surface (``POST /api/v1/events``) passes arbitrary
     caller JSON, so an event-type-agnostic payload match would let a
     forged ``coordinator_label`` steer events onto a coordinator.

  2. Otherwise we resolve ``plan_id`` → ``plans.repo`` →
     ``team_configs.coordinator_label`` (or ``task_id`` → ``plan`` →
     ``team_configs`` for task-scoped events that don't carry a
     ``plan_id``). These events are published post-commit, so the row
     exists.

Events that resolve to no coordinator (no ``team_configs`` row for the
repo, or an ownerless event with neither id) are dropped — the fabric
routes by ``coordinator_label``, so an event with no coordinator has no
inbox to land in. This is the same "ownerless events are dropped on
filtered connections" behavior the WS uses, so the fabric receives the
same set the WS coordinator socket would.

Failure isolation invariant
---------------------------

A slow or failing POST logs and continues; a DB lookup error logs and
drops the one event. The sink never raises into the eventbus broadcaster
and never blocks the publish path — a wedged fabric ingress must not
back-pressure event processing or the WS fan-out.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text

from treadmill_api.eventbus import subscribe_local, unsubscribe_local

logger = logging.getLogger("treadmill.coordination.fabric_event_sink")


_DEFAULT_TIMEOUT_SECONDS = 5.0
"""Per-POST timeout. Short enough that a wedged fabric ingress doesn't
build a backlog on the in-process queue; long enough that a healthy
ingress answers well inside the budget. Mirrors the notification
fan-out's budget."""


def _now_iso() -> str:
    """Offset-aware UTC ISO-8601 timestamp for the push ``ts`` field.

    Stamped at push time — the same convention the dashboard WS uses for
    its frame ``ts`` (the ``_build_record`` shape carries no event
    timestamp). The fabric treats this as the delivery time.
    """
    return datetime.now(timezone.utc).isoformat()


def _event_type(record: dict[str, Any]) -> str:
    """Compose the dotted ``entity_type.action`` event type.

    Yields the canonical forms the fabric expects — ``plan.submitted``,
    ``task.pr_merged``, ``check_run.completed``, etc.
    """
    return f"{record.get('entity_type')}.{record.get('action')}"


async def _resolve_coordinator_label(
    record: dict[str, Any],
    session_factory: Any,
) -> str | None:
    """Resolve the coordinator this event belongs to, or ``None``.

    See the module docstring for the two-branch resolution (plan.submitted
    payload fast path, then the ``team_configs`` DB lookup). Returns
    ``None`` when nothing resolves — the caller drops the event.

    Broken out as a module-level function so tests can monkeypatch or call
    it directly without spinning the asyncio loop.
    """
    entity_type = record.get("entity_type")
    action = record.get("action")
    payload = record.get("payload")

    # 1. plan.submitted payload fast path. Pre-commit-safe + gated to the
    #    documented carrier (mirrors the WS security posture).
    if (
        entity_type == "plan"
        and action == "submitted"
        and isinstance(payload, dict)
    ):
        label = payload.get("coordinator_label")
        if label:
            return str(label)

    # 2. DB path: plan_id -> team_configs, or task_id -> plan -> team_configs.
    if session_factory is None:
        return None
    plan_id = record.get("plan_id")
    task_id = record.get("task_id")
    if plan_id is None and task_id is None:
        return None
    async with session_factory() as session:
        if plan_id is not None:
            result = await session.execute(
                text(
                    "SELECT tc.coordinator_label "
                    "FROM plans p "
                    "JOIN team_configs tc ON tc.repo = p.repo "
                    "WHERE p.id = :id"
                ),
                {"id": uuid.UUID(str(plan_id))},
            )
        else:
            result = await session.execute(
                text(
                    "SELECT tc.coordinator_label "
                    "FROM tasks t "
                    "JOIN plans p ON p.id = t.plan_id "
                    "JOIN team_configs tc ON tc.repo = p.repo "
                    "WHERE t.id = :id"
                ),
                {"id": uuid.UUID(str(task_id))},
            )
        row = result.fetchone()
        return row[0] if row else None


class FabricEventSink:
    """Background subscriber that POSTs coordinator-fanned events to the
    exec_otp fabric HTTP ingress.

    Constructor takes the parsed config directly (not a ``Settings``
    instance) so tests can drive the sink against an ad-hoc URL + a stub
    session factory without round-tripping env parsing. ``make_fabric_event_sink``
    is the one place that builds it from ``Settings`` + the app's engine.

    Lifecycle mirrors ``NotificationFanout``: ``start()`` is a no-op on a
    no-URL build (returns without subscribing or spawning a task), and
    ``stop()`` is safe on a never-started instance.
    """

    def __init__(
        self,
        *,
        ingress_url: str | None = None,
        token: str | None = None,
        session_factory: Any = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # Normalize empty-string URL -> None so FABRIC_INGRESS_URL="" behaves
        # exactly like unset (dark by default).
        self.ingress_url = ingress_url or None
        # #236: the ingress is fail-closed; every POST must carry this bearer token.
        self._token = token or None
        self._session_factory = session_factory
        # An injected client (tests, shared) is left alone on shutdown; an
        # owned client is built lazily on start and closed in ``stop``.
        self._injected_client = http_client
        self._http_client: httpx.AsyncClient | None = http_client
        self._timeout_seconds = timeout_seconds
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def is_configured(self) -> bool:
        """True iff an ingress URL is set. Lets the lifespan handler skip
        ``start()`` (and thus the subscription) when the sink is dark."""
        return bool(self.ingress_url)

    async def start(self) -> None:
        """Subscribe to the eventbus broadcaster and spin the push loop.

        No-op when no ingress URL is configured — the sink ships dark, so
        an unset ``FABRIC_INGRESS_URL`` means no subscription and no task.
        """
        if not self.is_configured:
            logger.info(
                "fabric event sink: FABRIC_INGRESS_URL unset; sink is dark, "
                "skipping start"
            )
            return
        if self._token is None:
            # Observability (carla #235 review): a live-but-untokened sink pushes to the #236
            # fail-closed ingress, which 401s every POST — and the sink swallows failures, so the
            # drop is otherwise invisible. Surface it once at start so "events just stopped" is diagnosable.
            logger.warning(
                "fabric event sink: FABRIC_INGRESS_URL set but FABRIC_INGRESS_TOKEN unset — the "
                "fail-closed ingress (#236) will 401 every push; events will be silently dropped"
            )
        self._stopped = False
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                # #236: the ingress is fail-closed — without the bearer token every POST 401s.
                headers={"Authorization": f"Bearer {self._token}"} if self._token else {},
            )
        # Subscribe BEFORE spawning the consumer task so the queue exists
        # by the time the task awaits ``queue.get()``.
        self._queue = subscribe_local()
        self._task = asyncio.create_task(
            self._run(), name="fabric-event-sink",
        )
        logger.info(
            "fabric event sink started: pushing coordinator-fanned events "
            "to the configured fabric ingress"
        )

    async def stop(self) -> None:
        """Tear down the loop + unsubscribe + close the owned http client.

        Safe to call on a never-started instance (dark build): the early
        returns mirror the no-op start path.
        """
        self._stopped = True
        if self._queue is not None:
            unsubscribe_local(self._queue)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("fabric event sink raised on shutdown")
            self._task = None
        self._queue = None
        # Only close the client we own; an injected one is the caller's.
        if self._injected_client is None and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("fabric event sink stopped")

    async def _run(self) -> None:
        assert self._queue is not None  # for the type-checker; set in start()
        while not self._stopped:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self.handle(record)
            except Exception:
                # Belt-and-braces — ``handle`` already catches per-event
                # failures, but a bug in the resolution logic must never
                # take the loop down.
                logger.exception(
                    "fabric event sink: handler raised; continuing",
                )

    async def handle(self, record: dict[str, Any]) -> None:
        """Public entry point — exposed so tests can drive the push without
        spinning the asyncio loop.

        Resolves the coordinator for the event and, when one resolves,
        POSTs the ``{coordinator_label, event_type, payload, ts}`` body to
        the ingress. Ownerless / no-coordinator events are dropped. All
        failures (DB lookup, POST) are caught here and never propagated.
        """
        if not self.ingress_url:
            return
        if self._http_client is None:
            # Defensive: ``start()`` builds the client. If we got here
            # without one, the sink wasn't started — drop quietly.
            logger.warning(
                "fabric event sink: handle called without http client; "
                "skipping record event_id=%s",
                record.get("event_id"),
            )
            return

        try:
            coordinator_label = await _resolve_coordinator_label(
                record, self._session_factory,
            )
        except Exception:
            logger.exception(
                "fabric event sink: coordinator_label lookup failed for "
                "event_id=%s (%s); dropping event",
                record.get("event_id"),
                _event_type(record),
            )
            return

        if not coordinator_label:
            # No coordinator owns this event — nothing to route it to.
            return

        body = {
            "coordinator_label": coordinator_label,
            "event_type": _event_type(record),
            "payload": record.get("payload"),
            "ts": _now_iso(),
        }
        try:
            await self._http_client.post(self.ingress_url, json=body)
        except Exception:
            logger.exception(
                "fabric event sink: POST to ingress failed for "
                "event_id=%s (%s, coordinator=%s); continuing",
                record.get("event_id"),
                _event_type(record),
                coordinator_label,
            )


def make_fabric_event_sink(
    settings: Any,
    session_factory: Any = None,
) -> FabricEventSink:
    """Build a ``FabricEventSink`` from a ``Settings`` instance.

    Pulled out of the lifespan handler so the construction path is
    testable on its own and the lifespan stays focused on wiring. The
    ``session_factory`` is the app's ``async_sessionmaker`` (or ``None``
    when no engine is wired — the sink then resolves only the
    ``plan.submitted`` payload fast path).
    """
    return FabricEventSink(
        ingress_url=settings.fabric_ingress_url,
        token=getattr(settings, "fabric_ingress_token", None),
        session_factory=session_factory,
    )
