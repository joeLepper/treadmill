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
* DURABLE by RECONCILIATION, not by the queue. ``subscribe_local`` is an in-process asyncio
  queue — a fast path, not a durable one: a wake published while this consumer is down, or
  lost when a crash lands between the per-dependent commits, is gone from that queue with no
  redelivery, which would REINTRODUCE the wake-gap (Bert's review, 2026-09-15). So the live
  event path is backed by a periodic RECONCILE SWEEP (``reconcile``) over the DURABLE facts:
  it dispatches any router-substrate task whose ``depends_on`` is satisfied but which has NO
  author ``task_executions`` row at its current generation. That closes the down-consumer and
  crash-mid-loop holes — and, because it reads the DB rather than one process's queue, the
  cross-process/multi-replica miss too (a webhook persisted on another replica). The sweep is
  idempotent with the live path via the same ``(task_id, generation)`` unique index, so the
  two never double-dispatch. SC2 ("wake-gap cannot recur") holds on the sweep, not on the
  consumer being perfectly live.

DEPLOYMENT: the live event path assumes ONE designated consumer process (in-process bus);
the reconcile sweep is what makes correctness independent of that assumption.

Lifecycle mirrors ``FabricEventSink``/``NotificationFanout``: ``start()`` is a no-op on a dark
build (router dispatch disabled), ``stop()`` is safe on a never-started instance.

INCREMENT 1 (this file): the ``github.pr_merged`` → dependent-dispatch path + the reconcile
sweep. Follow-on increments (flagged inline): ``task.ci_result`` → re-eval dispatch (via
``is_ci_ready``, Bert), and the evaluator ``rework`` verdict → generation-bump + author
re-dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from treadmill_api.coordination.dispatch_predicates import (
    is_depends_on_satisfied,
    on_ci_result,
)
from treadmill_api.dispatch import Dispatcher
from treadmill_api.eventbus import subscribe_local, unsubscribe_local
from treadmill_api.events.task import TaskReady
from treadmill_api.models.evaluator_dispatch import EvaluatorDispatch
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

    def __init__(
        self,
        *,
        session_factory: Any = None,
        dispatcher: Dispatcher | None = None,
        enabled: bool = False,
        reconcile_interval_seconds: float = 30.0,
    ) -> None:
        # Dark by default: an unset router-dispatch flag means no subscription and no task,
        # exactly like FabricEventSink's no-URL build.
        self._session_factory = session_factory
        # The durable launch path: persist_and_publish a task.ready event (source-of-truth
        # Event row + bus publish, replay-resilient) so the assigned worker session — which
        # subscribes by its label — picks the task up. Without a dispatcher the consumer still
        # records the dispatch row (SC2/SC3) but does not emit the launch signal.
        self._dispatcher = dispatcher
        self._enabled = enabled and session_factory is not None
        self._reconcile_interval = reconcile_interval_seconds
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
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
        # The reconcile sweep is the durable backstop for the in-process queue (see the
        # module docstring): it makes SC2 hold even across a down consumer or a crash mid-loop.
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="dispatch-reconcile"
        )
        logger.info("dispatch consumer started: router owns dispatch for router-substrate plans")

    async def stop(self) -> None:
        self._stopped = True
        if self._queue is not None:
            unsubscribe_local(self._queue)
        for attr in ("_task", "_reconcile_task"):
            t: asyncio.Task[None] | None = getattr(self, attr)
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("dispatch consumer raised on shutdown")
                setattr(self, attr, None)
        self._queue = None
        logger.info("dispatch consumer stopped")

    async def _reconcile_loop(self) -> None:
        """Periodically run the reconcile sweep — the durable backstop for the in-process
        queue. Any failure is contained; the loop must outlive a transient DB hiccup."""
        while not self._stopped:
            try:
                await asyncio.sleep(self._reconcile_interval)
            except asyncio.CancelledError:
                raise
            if self._stopped or self._session_factory is None:
                continue
            try:
                async with self._session_factory() as session:
                    await self.reconcile(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dispatch consumer: reconcile sweep raised; continuing")

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
            elif key == ("task", "ci_result"):
                # TRACE 2 — the ci_result wake. Gate substrate here (Bert's split: the consumer
                # gates, on_ci_result decides), then let the handler fire the evaluator when the
                # required checks are ready. The per-(task, head) dedup lives in the write.
                task_id = record.get("task_id")
                if task_id and await self._is_router_task(session, str(task_id)):
                    await on_ci_result(session, record, self._dispatch_evaluator)
            # Follow-on increment (recognized, not yet acting):
            #   ("task", "evaluator_verdict")-> rework → bump generation + re-dispatch author
            elif key in {("run", "completed"), ("task", "completed")}:
                await self._on_upstream_terminal(session, record)

    async def reconcile(self, session: Any) -> int:
        """The durable backstop: dispatch every router-substrate task whose deps are satisfied
        but which has NO author ``task_executions`` row at its current generation. Idempotent
        with the live path via the ``(task_id, generation)`` unique index. Returns the count
        dispatched (0 in steady state). This — not the in-process queue — is why SC2 holds
        across a down consumer, a crash mid-loop, or a cross-replica publish.

        Exposed (like ``handle``) so tests can drive one sweep without the timer loop.
        """
        candidates = (
            await session.execute(
                text(
                    "SELECT t.id FROM tasks t "
                    "JOIN plans p ON p.id = t.plan_id "
                    "WHERE p.substrate = 'router' "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM task_executions te "
                    "    WHERE te.task_id = t.id AND te.generation = t.generation "
                    "      AND te.trigger IN ('initial','coordinator-rework','evaluator-rework')"
                    "  )"
                    # A task whose PR has merged is DONE — never re-dispatch it, even if it
                    # somehow lacks a current-generation execution row (e.g. an upstream that
                    # completed). The live path dispatches dependents, not the merged task; the
                    # sweep scans all tasks, so it needs this terminal guard.
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM events e "
                    "    WHERE e.task_id = t.id AND e.action = 'pr_merged'"
                    "  )"
                )
            )
        ).all()
        dispatched = 0
        for (task_id,) in candidates:
            if await self._maybe_dispatch(session, str(task_id)):
                dispatched += 1
        if dispatched:
            logger.info("dispatch consumer: reconcile sweep dispatched %d task(s)", dispatched)
        return dispatched

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

    async def _maybe_dispatch(self, session: Any, task_id: str) -> bool:
        """Dispatch ``task_id`` iff it is on the router substrate and every dependency is
        satisfied. Idempotent: a second delivery for the same (task, generation) no-ops on the
        unique index. Returns True iff a new dispatch row was created."""
        task = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            return False
        if not await self._is_router_plan(session, task.plan_id):
            return False  # SC6 — the legacy coordinator owns non-router plans.
        if not await is_depends_on_satisfied(session, task_id):
            return False
        return await self._dispatch(session, task, trigger="initial")

    async def _is_router_plan(self, session: Any, plan_id: Any) -> bool:
        substrate = (
            await session.execute(select(Plan.substrate).where(Plan.id == plan_id))
        ).scalar_one_or_none()
        return substrate == "router"

    async def _is_router_task(self, session: Any, task_id: str) -> bool:
        """SC6 gate for a task-scoped event that carries only ``task_id`` (e.g. ci_result):
        the task's plan must be on the router substrate."""
        substrate = (
            await session.execute(
                select(Plan.substrate).join(Task, Task.plan_id == Plan.id).where(
                    Task.id == task_id
                )
            )
        ).scalar_one_or_none()
        return substrate == "router"

    async def _dispatch_evaluator(self, *, task_id: str, head_sha: str) -> bool:
        """The WRITE half of the ci_result→re-eval path (injected into ``on_ci_result``).

        Records ONE evaluator dispatch per (task, head): the UNIQUE(task_id, head_sha) makes a
        trailing non-required suite's re-entry a no-op, so the evaluator fires exactly once per
        head. A rework push is a new head_sha → a new evaluation. Runs in its own session (the
        handler passes no session). Returns True iff a new dispatch was recorded.
        """
        if self._session_factory is None:
            return False
        async with self._session_factory() as session:
            session.add(EvaluatorDispatch(task_id=task_id, head_sha=head_sha))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.debug(
                    "dispatch consumer: evaluator already dispatched task=%s head=%s (no-op)",
                    task_id,
                    head_sha,
                )
                return False
            logger.info(
                "dispatch consumer: evaluator dispatched task=%s head=%s", task_id, head_sha
            )
            return True

    async def _dispatch(self, session: Any, task: Task, *, trigger: str) -> bool:
        """Record ONE author dispatch for ``task`` at its current generation. Returns True if
        a new row was created, False if the unique index no-oped a re-delivery.

        The ``task_executions`` row IS the dispatch record SC2 asserts appears deterministically
        on a delivered event. Worker assignment policy (which live worker) is a follow-on; the
        label is deterministic here so the record + its idempotency are exercisable now.
        """
        worker_label = self._resolve_worker(task)
        try:
            # SAVEPOINT: the partial UNIQUE (task_id, generation) violation surfaces at flush
            # OR at commit; a nested transaction contains it so a re-delivery rolls back to the
            # savepoint WITHOUT poisoning the outer transaction or the loop's other dependents.
            async with session.begin_nested():
                session.add(
                    TaskExecution(
                        task_id=task.id,
                        worker_label=worker_label,
                        trigger=trigger,
                        generation=task.generation,
                    )
                )
                await session.flush()
        except IntegrityError:
            # Already an author dispatch for this cycle — a re-delivered event. Idempotent
            # no-op by construction; the savepoint rolled back, the outer txn is intact.
            logger.debug(
                "dispatch consumer: %s already dispatched at generation %s (re-delivery no-op)",
                task.id,
                task.generation,
            )
            return False
        # LAUNCH: the dispatch row landed — emit task.ready in the SAME transaction so the row
        # and the durable Event commit atomically. The assigned worker (subscribed by label)
        # consumes it and fetches its brief. No dispatcher (dark/test) -> record only, no launch.
        if self._dispatcher is not None:
            await self._dispatcher.persist_and_publish(
                session,
                entity_type="task",
                action="ready",
                payload=TaskReady(),
                task_id=task.id,
            )
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


def make_dispatch_consumer(
    settings: Any, session_factory: Any = None, publisher: Any = None
) -> DispatchConsumer:
    """Build a DispatchConsumer from Settings + the app's EventPublisher. Dark unless
    ``ROUTER_DISPATCH_ENABLED`` is set, mirroring ``make_fabric_event_sink`` — the router ships
    behind a flag so a repo runs the router or the legacy coordinator during cutover (ADR-0118
    rollout). The publisher (``app.state.publisher``) drives the durable task.ready launch."""
    enabled = bool(getattr(settings, "router_dispatch_enabled", False))
    dispatcher = Dispatcher(publisher) if publisher is not None else None
    return DispatchConsumer(
        session_factory=session_factory, dispatcher=dispatcher, enabled=enabled
    )
