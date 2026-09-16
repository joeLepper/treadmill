"""Worker dispatch sink — ADR-0118 phase 2, the router→worker delivery bridge.

The router records a dispatch (a ``task_executions`` row) and emits ``task.ready`` (the launch
signal). This sink is the WORKER half of delivery: on a ``task.ready`` for a ROUTER-substrate
task, it resolves the assigned worker and POSTs the dispatch to the fabric ingress, which wakes
that worker's long-lived session (ADR-0089). The worker then reads its brief and works — the
worker template already consumes a delivered brief, so no worker-side code change is needed;
this replaces the agent coordinator's ``send``-the-brief step with a server-side delivery.

It mirrors ``FabricEventSink`` exactly (same lifecycle, same ingress POST shape) — the only
difference is the RESOLVER: FabricEventSink routes an event to the plan's COORDINATOR by
``coordinator_label``; this routes a ``task.ready`` to the assigned WORKER by the
``task_executions.worker_label`` the router just wrote. Legacy-substrate plans still flow
through FabricEventSink to their agent coordinator; this sink acts ONLY on router plans, so the
two never both deliver.

Dark by default: no ingress URL configured -> ``start()`` is a no-op (same as FabricEventSink).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 10.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _resolve_worker_dispatch(
    record: dict[str, Any], session_factory: Any
) -> tuple[str, str] | None:
    """For a ``task.ready`` on a ROUTER-substrate task, return (worker_label, task_id); else
    None. The worker is the label the router wrote on the task's newest ``task_executions`` row
    (its current dispatch). Broken out module-level so tests drive it without the loop."""
    if record.get("entity_type") != "task" or record.get("action") != "ready":
        return None
    task_id = record.get("task_id")
    if not task_id or session_factory is None:
        return None
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT te.worker_label "
                    "FROM task_executions te "
                    "JOIN tasks t ON t.id = te.task_id "
                    "JOIN plans p ON p.id = t.plan_id "
                    "WHERE te.task_id = :tid AND p.substrate = 'router' "
                    "  AND te.trigger IN ('initial','coordinator-rework','evaluator-rework') "
                    "ORDER BY te.started_at DESC LIMIT 1"
                ),
                {"tid": str(task_id)},
            )
        ).first()
    if row is None:
        return None  # not a router task, or no author dispatch — nothing to deliver.
    return str(row[0]), str(task_id)


class WorkerDispatchSink:
    """Background subscriber that delivers router ``task.ready`` dispatches to the assigned
    worker's session via the fabric ingress. Mirrors ``FabricEventSink``."""

    def __init__(
        self,
        *,
        ingress_url: str | None = None,
        session_factory: Any = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        enabled: bool = True,
    ) -> None:
        # Class-level darkness (Bert review): the router flag gates start() here, not only in
        # the factory, so a direct construction on a legacy fleet can't start either. handle()
        # is still callable directly (tests), guarded by ingress_url/http_client.
        self._enabled = enabled
        self.ingress_url = ingress_url or None
        self._session_factory = session_factory
        self._injected_client = http_client
        self._http_client: httpx.AsyncClient | None = http_client
        self._timeout_seconds = timeout_seconds
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def is_configured(self) -> bool:
        return self._enabled and bool(self.ingress_url)

    async def start(self) -> None:
        if not self.is_configured:
            logger.info("worker dispatch sink: disabled or no ingress URL; dark, skipping start")
            return
        from treadmill_api.eventbus import subscribe_local

        self._stopped = False
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout_seconds)
        self._queue = subscribe_local()
        self._task = asyncio.create_task(self._run(), name="worker-dispatch-sink")
        logger.info("worker dispatch sink started: delivering router task.ready to workers")

    async def stop(self) -> None:
        from treadmill_api.eventbus import unsubscribe_local

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
                logger.exception("worker dispatch sink raised on shutdown")
            self._task = None
        self._queue = None
        if self._injected_client is None and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("worker dispatch sink stopped")

    async def _run(self) -> None:
        assert self._queue is not None
        while not self._stopped:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self.handle(record)
            except Exception:
                logger.exception("worker dispatch sink: handle raised; continuing")

    async def handle(self, record: dict[str, Any]) -> None:
        """Resolve the assigned worker for a router ``task.ready`` and POST the dispatch to the
        ingress. Non-task.ready / non-router / unresolved events are dropped. All failures are
        caught here and never propagated (mirrors FabricEventSink)."""
        if not self.ingress_url or self._http_client is None:
            return
        try:
            resolved = await _resolve_worker_dispatch(record, self._session_factory)
        except Exception:
            logger.exception(
                "worker dispatch sink: resolve failed for task_id=%s; dropping",
                record.get("task_id"),
            )
            return
        if resolved is None:
            return
        worker_label, task_id = resolved
        body = {
            "worker_label": worker_label,
            "event_type": "task.ready",
            "payload": {"task_id": task_id},
            "ts": _now_iso(),
        }
        try:
            await self._http_client.post(self.ingress_url, json=body)
        except Exception:
            logger.exception(
                "worker dispatch sink: POST to ingress failed for task_id=%s worker=%s; "
                "the reconcile sweep + a worker heartbeat still recover it",
                task_id,
                worker_label,
            )


def make_worker_dispatch_sink(settings: Any, session_factory: Any = None) -> WorkerDispatchSink:
    """Build the sink from Settings. Reuses the same fabric ingress URL FabricEventSink uses —
    the ingress routes by the label in the body, so the worker path needs no separate ingress.
    Dark unless ROUTER_DISPATCH_ENABLED: with the router off, no router task.ready events are
    emitted anyway, so gating start on the flag keeps an idle subscriber out of a legacy fleet."""
    enabled = bool(getattr(settings, "router_dispatch_enabled", False))
    ingress_url = getattr(settings, "fabric_ingress_url", None) if enabled else None
    return WorkerDispatchSink(
        ingress_url=ingress_url, session_factory=session_factory, enabled=enabled
    )
