"""Event→dispatch consumer — ADR-0118 phase 1, the wake-gap fix.

Phase-0 found the root of the recurring wake-gap: the server DELIVERS every task-scoped
completion event (``github.pr_merged``, ``task.ci_result``, evaluator verdicts) but nothing
server-side ACTS on a delivered event — only the coordinator AGENT's turn does, so a wake is
lost whenever that turn is idle. This background subscriber closes it by construction: it
consumes each delivered event and does the next dispatch deterministically, with NO agent
turn to miss.

Design (ADR-0118):
* One dispatch WRITER (this consumer). The judgment stays in agents — ``is_depends_on_satisfied``
  and ``is_ci_ready`` (``dispatch_predicates``) are pure decision functions this calls; the
  workers + validator keep peer review / verdicts.
* Idempotent by construction: a dispatch is an INSERT of a ``task_executions`` row at the
  task's current ``generation``; the partial UNIQUE index ``(task_id, generation)`` over
  author triggers makes a re-delivered event a database no-op (migration 20260915_0100). A
  genuine rework bumps ``tasks.generation`` first, so the next dispatch is a new row.
* Per-plan substrate (SC6): acts ONLY on plans whose ``substrate == 'router'``; the legacy
  agent-coordinator owns the rest. The binding is read per event but is immutable once set at
  standup, so there is no cross-substrate double-dispatch.

Lifecycle mirrors ``FabricEventSink``/``NotificationFanout``: ``start()`` is a no-op on a dark
build (router dispatch disabled), ``stop()`` is safe on a never-started instance.

INCREMENT 1 (this file): the ``github.pr_merged`` → dependent-dispatch path — the clearest
``depends_on`` edge. Follow-on increments (flagged inline): ``task.ci_result`` → re-eval
dispatch (via ``is_ci_ready``), and the evaluator ``rework`` verdict → generation-bump +
author re-dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from treadmill_api.coordination.dispatch_predicates import is_depends_on_satisfied
from treadmill_api.eventbus import subscribe_local, unsubscribe_local
from treadmill_api.models.plan import Plan
from treadmill_api.models.task import Task
from treadmill_api.models.task_execution import TaskExecution

logger = logging.getLogger(__name__)

# The task-scoped completion events that can unblock or re-trigger work. Increment 1 acts on
# pr_merged; the others are recognized and routed to their (stubbed) handlers so the classify
# path is complete and testable now.
_HANDLED = {
    ("github", "pr_merged"),
    ("task", "ci_result"),
    ("task", "evaluator_verdict"),
    ("run", "completed"),
    ("task", "completed"),
}


class DispatchConsumer:
    """Background eventbus subscriber that turns a delivered completion event into the next
    deterministic dispatch. See the module docstring for the ADR-0118 design."""

    def __init__(self, *, session_factory: Any = None, enabled: bool = False) -> None:
        # Dark by default: an unset router-dispatch flag means no subscription and no task,
        # exactly like FabricEventSink's no-URL build.
        self._session_factory = session_factory
        self._enabled = enabled and session_factory is not None
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def is_configured(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if not self._enabled:
            logger.info(
                "dispatch consumer: router dispatch disabled; consumer is dark, skipping start"
            )
            return
        self._stopped = False
        # Subscribe BEFORE spawning the task so the queue exists when the task awaits get().
        self._queue = subscribe_local()
        self._task = asyncio.create_task(self._run(), name="dispatch-consumer")
        logger.info("dispatch consumer started: router owns dispatch for router-substrate plans")

    async def stop(self) -> None:
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
                logger.exception("dispatch consumer raised on shutdown")
            self._task = None
        self._queue = None
        logger.info("dispatch consumer stopped")

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
                # A bug in the dispatch logic must never take the loop down (mirrors
                # FabricEventSink). Per-event failures are contained here.
                logger.exception("dispatch consumer: handle raised; continuing")

    async def handle(self, record: dict[str, Any]) -> None:
        """Public entry point (exposed for tests, no asyncio loop needed).

        Classifies the event and routes to the matching dispatch handler. Non-completion
        events and events for non-router plans are dropped.
        """
        key = (record.get("entity_type"), record.get("action"))
        if key not in _HANDLED:
            return
        if self._session_factory is None:
            return

        async with self._session_factory() as session:
            if key == ("github", "pr_merged"):
                await self._on_upstream_terminal(session, record)
            # Follow-on increments (recognized, not yet acting):
            #   ("task", "ci_result")        -> is_ci_ready → dispatch the evaluator
            #   ("task", "evaluator_verdict")-> rework → bump generation + re-dispatch author
            #   ("run", "completed") / ("task","completed") -> same terminal-edge path below
            elif key in {("run", "completed"), ("task", "completed")}:
                await self._on_upstream_terminal(session, record)

    async def _on_upstream_terminal(self, session: Any, record: dict[str, Any]) -> None:
        """An upstream task reached a terminal fact (pr_merged / run.completed / completed):
        dispatch every dependent whose ``depends_on`` is now fully satisfied."""
        upstream_id = record.get("task_id")
        if not upstream_id:
            return
        upstream_id = str(upstream_id)

        # Candidate dependents: tasks whose dependency expression references this upstream id.
        # (Expressions are stored with the upstream's resolved UUID, per routers/plans.py.)
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT task_id FROM task_dependencies "
                    "WHERE expression LIKE '%' || :uid || '%'"
                ),
                {"uid": upstream_id},
            )
        ).all()
        for (dep_task_id,) in rows:
            await self._maybe_dispatch(session, str(dep_task_id))

    async def _maybe_dispatch(self, session: Any, task_id: str) -> None:
        """Dispatch ``task_id`` iff it is on the router substrate and every dependency is
        satisfied. Idempotent: a second delivery for the same (task, generation) no-ops on the
        unique index."""
        task = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            return
        if not await self._is_router_plan(session, task.plan_id):
            return  # SC6 — the legacy coordinator owns non-router plans.
        if not await is_depends_on_satisfied(session, task_id):
            return
        await self._dispatch(session, task, trigger="initial")

    async def _is_router_plan(self, session: Any, plan_id: Any) -> bool:
        substrate = (
            await session.execute(select(Plan.substrate).where(Plan.id == plan_id))
        ).scalar_one_or_none()
        return substrate == "router"

    async def _dispatch(self, session: Any, task: Task, *, trigger: str) -> bool:
        """Record ONE author dispatch for ``task`` at its current generation. Returns True if
        a new row was created, False if the unique index no-oped a re-delivery.

        The ``task_executions`` row IS the dispatch record SC2 asserts appears deterministically
        on a delivered event. Worker assignment policy (which live worker) is a follow-on; the
        label is deterministic here so the record + its idempotency are exercisable now.
        """
        worker_label = self._resolve_worker(task)
        execution = TaskExecution(
            task_id=task.id,
            worker_label=worker_label,
            trigger=trigger,
            generation=task.generation,
        )
        session.add(execution)
        try:
            await session.flush()
        except IntegrityError:
            # The partial UNIQUE (task_id, generation) already holds an author dispatch for
            # this cycle — a re-delivered event. Idempotent no-op by construction.
            await session.rollback()
            logger.debug(
                "dispatch consumer: %s already dispatched at generation %s (re-delivery no-op)",
                task.id,
                task.generation,
            )
            return False
        await session.commit()
        logger.info(
            "dispatch consumer: dispatched task=%s generation=%s trigger=%s worker=%s",
            task.id,
            task.generation,
            trigger,
            worker_label,
        )
        return True

    @staticmethod
    def _resolve_worker(task: Task) -> str:
        """Deterministic worker label for a dispatch. Phase-1 placeholder: a stable
        per-task label so the dispatch record + idempotency are exercisable. The worker-pool
        assignment policy (round-robin over a repo's live workers) is a follow-on slice."""
        return f"router-worker-{task.repo}"


def make_dispatch_consumer(settings: Any, session_factory: Any = None) -> DispatchConsumer:
    """Build a DispatchConsumer from Settings. Dark unless ``ROUTER_DISPATCH_ENABLED`` is set,
    mirroring ``make_fabric_event_sink`` — the router ships behind a flag so a repo runs the
    router or the legacy coordinator during cutover (ADR-0118 rollout)."""
    enabled = bool(getattr(settings, "router_dispatch_enabled", False))
    return DispatchConsumer(session_factory=session_factory, enabled=enabled)
