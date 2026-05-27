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
                    ``action``, ``task_id``, ``ts``, ``id``).
  * ``heartbeat`` — every ``heartbeat_interval`` seconds. Lets a client
                    detect a dead socket faster than TCP keepalive does.

Backpressure: ``send_json`` is wrapped in ``asyncio.wait_for`` with a
1 s budget. A blocked send means the client (or the network in front of
it) can't keep up; we'd rather drop and let the client reconnect than
let one slow consumer wedge the publish loop's in-process queue.

The heartbeat interval is configurable so tests don't have to wait 25 s
to observe one. Production callers (the auto-discovery loop) get the
default.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

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
