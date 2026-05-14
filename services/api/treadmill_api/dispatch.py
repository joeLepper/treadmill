"""Dispatch — turn a Task into a WorkflowRun + ready first step.

When a Task lands in the system with its parent Plan active, the API
materializes a ``WorkflowRun``, snapshots the workflow version's steps
as ``WorkflowRunStep`` rows, and publishes ``step.ready`` for the first
step. That event lands on the events SNS topic (audit) and a thin claim
message lands on the work SQS queue (the autoscaler trigger).

Per ADR-0011 the dispatcher is the *only* synchronous side of step
creation: every subsequent transition arrives via the coordination
consumer reading lifecycle events. The dispatch path produces durable
state (Event row + run + steps) before any external publish so a partial
publish leaves consistent DB state.

The ``_persist_and_publish`` helper is the shared seam every emitter
uses: it INSERTs the Event row, flushes, and fires ``publisher.publish``
in a try/except. Publish failure is logged and (for the dispatcher path)
recorded as a ``DispatchPublishFailed`` Event-row marker (A.8); the
replay loop (A.10) reads those markers on a 30s tick and re-publishes.

Gating
------

Two gates run before ``dispatch_task`` actually publishes the
``step.ready`` event + SQS claim:

  * **Plan-active gate (D.5).** The task's parent plan must resolve to
    ``derived_status='active'`` in the ``plan_status`` VIEW. If not
    (drafting / planning), the dispatcher still persists the
    WorkflowRun + steps (so the run graph is complete and the consumer's
    re-evaluation pass can find it), but skips publish + send.

  * **Dependency gate (D.2).** Each row in ``task_dependencies`` is
    evaluated against the current event log + run state. Unsatisfied
    dependencies short-circuit the same way as the plan-active gate.

Re-entry via the consumer's re-evaluation pass (D.6) is idempotent: the
top of ``dispatch_task`` checks for an existing ``step.ready`` event on
the task and short-circuits if found, returning the original run id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.eventbus import EventPublisher
from treadmill_api.events import EventPayload
from treadmill_api.events.internal import DispatchPublishFailed
from treadmill_api.events.step import StepReady
from treadmill_api.models import (
    Event,
    Task,
    TaskDependency,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
    WorkflowVersionStep,
)

logger = logging.getLogger("treadmill.dispatch")


class DispatchError(RuntimeError):
    """Raised when a task cannot be dispatched (e.g. its workflow version
    has no steps). Surfaced to the HTTP caller as a 400 — it indicates a
    misconfigured workflow, not a server fault."""


class DependencyExpressionError(ValueError):
    """A ``task_dependencies.expression`` row was not parseable under the
    v0 grammar. The plans router 400s on input before persistence, so this
    only fires when a row was inserted by a different path (test seeding,
    a future migration, etc.). The dispatcher treats it as a "do not
    dispatch yet" failure so a malformed row cannot accidentally release
    work — the operator must fix the row."""


# ── Dependency evaluation helpers (D.2) ───────────────────────────────────────
#
# Grammar (mirrors ``routers/plans.py`` post-substitution):
#
#   task.<uuid>.pr_merged
#   task.<uuid>.run.completed
#   task.<uuid>.step.<name>.completed
#
# These are evaluated by ``evaluate_dependencies`` against the current DB
# state. Both the dispatcher and (in Phase 3 D.6) the consumer's
# re-evaluation pass call this — keeping the evaluator at module level so
# both sides share the exact same semantics.

_DEP_PR_MERGED_RE = re.compile(
    r"^task\.(?P<uuid>[0-9a-fA-F-]{36})\.pr_merged$"
)
_DEP_RUN_COMPLETED_RE = re.compile(
    r"^task\.(?P<uuid>[0-9a-fA-F-]{36})\.run\.completed$"
)
_DEP_STEP_COMPLETED_RE = re.compile(
    r"^task\.(?P<uuid>[0-9a-fA-F-]{36})\.step\.(?P<name>[a-zA-Z0-9_-]+)\.completed$"
)


async def _is_dep_pr_merged(
    session: AsyncSession, sibling_task_id: uuid.UUID,
) -> bool:
    """``task.<uuid>.pr_merged`` — true iff a ``github.pr_merged`` event
    exists for that task's PR. The webhook handler (or test seed) writes
    that event with ``task_id`` populated."""
    result = await session.execute(
        select(func.count(Event.id)).where(
            Event.task_id == sibling_task_id,
            Event.entity_type == "github",
            Event.action == "pr_merged",
        )
    )
    return (result.scalar_one() or 0) > 0


async def _is_dep_run_completed(
    session: AsyncSession, sibling_task_id: uuid.UUID,
) -> bool:
    """``task.<uuid>.run.completed`` — true iff there's a WorkflowRun on
    that task and *all* of its steps are ``completed``. A run with zero
    steps does not satisfy (it's a degenerate run that never advances)."""
    runs = (await session.execute(
        select(WorkflowRun.id).where(WorkflowRun.task_id == sibling_task_id)
    )).scalars().all()
    if not runs:
        return False
    for run_id in runs:
        steps = (await session.execute(
            select(WorkflowRunStep.status).where(WorkflowRunStep.run_id == run_id)
        )).scalars().all()
        if not steps:
            continue
        if all(s == "completed" for s in steps):
            return True
    return False


async def _is_dep_step_completed(
    session: AsyncSession, sibling_task_id: uuid.UUID, step_name: str,
) -> bool:
    """``task.<uuid>.step.<name>.completed`` — true iff there's a
    ``WorkflowRunStep`` with ``step_name`` and ``status='completed'``
    belonging to a run on that task."""
    result = await session.execute(
        select(func.count(WorkflowRunStep.id))
        .join(WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id)
        .where(
            WorkflowRun.task_id == sibling_task_id,
            WorkflowRunStep.step_name == step_name,
            WorkflowRunStep.status == "completed",
        )
    )
    return (result.scalar_one() or 0) > 0


async def evaluate_dependency_expression(
    session: AsyncSession, expression: str,
) -> bool:
    """Evaluate a single ``task_dependencies.expression`` row against the
    current DB state. Returns ``True`` iff the dependency is satisfied.

    Raises ``DependencyExpressionError`` if the expression doesn't parse
    — the dispatcher treats parse failures as unsatisfied so a malformed
    row cannot leak work, but it logs at WARNING so ops sees the bad row.
    """
    m = _DEP_PR_MERGED_RE.match(expression)
    if m is not None:
        return await _is_dep_pr_merged(session, uuid.UUID(m.group("uuid")))
    m = _DEP_RUN_COMPLETED_RE.match(expression)
    if m is not None:
        return await _is_dep_run_completed(session, uuid.UUID(m.group("uuid")))
    m = _DEP_STEP_COMPLETED_RE.match(expression)
    if m is not None:
        return await _is_dep_step_completed(
            session, uuid.UUID(m.group("uuid")), m.group("name"),
        )
    raise DependencyExpressionError(
        f"malformed task_dependencies expression: {expression!r}"
    )


async def evaluate_dependencies(
    session: AsyncSession, task_id: uuid.UUID,
) -> bool:
    """Return ``True`` iff *every* ``task_dependencies`` row for the task
    is satisfied. Empty dependency set is trivially satisfied.

    A malformed row blocks dispatch (returns ``False``) and logs at
    WARNING — the consumer's re-evaluation pass (D.6) will retry on each
    event, so an operator-fixed row picks up next pass.
    """
    rows = (await session.execute(
        select(TaskDependency.expression).where(TaskDependency.task_id == task_id)
    )).scalars().all()
    for expr in rows:
        try:
            ok = await evaluate_dependency_expression(session, expr)
        except DependencyExpressionError:
            logger.warning(
                "task %s blocked by malformed dependency expression %r; "
                "fix the row to unblock dispatch", task_id, expr,
            )
            return False
        if not ok:
            return False
    return True


# ── Plan-active gate helper (D.5) ─────────────────────────────────────────────


async def is_plan_active(session: AsyncSession, plan_id: uuid.UUID) -> bool:
    """Read ``plan_status.derived_status`` for a plan. Returns ``True``
    iff the VIEW resolves to ``active``.

    The VIEW reads from ``events`` — the dispatcher flushes the session
    before this call, so Postgres' read-your-own-writes semantics apply:
    a ``plan.activated`` event INSERTed earlier in the same transaction
    is visible here (Scenario 1 plan creation depends on this).
    """
    result = await session.execute(
        sa_text("SELECT derived_status FROM plan_status WHERE id = :id"),
        {"id": plan_id},
    )
    row = result.first()
    if row is None:
        return False
    return row.derived_status == "active"


# ── Dispatcher ────────────────────────────────────────────────────────────────


class Dispatcher:
    """Bundles the I/O dependencies needed to dispatch a task. One instance
    per request, constructed via the ``get_dispatcher`` FastAPI dependency."""

    def __init__(
        self,
        publisher: EventPublisher,
        sqs_client: Any | None,
        work_queue_url: str | None,
    ) -> None:
        self.publisher = publisher
        self.sqs_client = sqs_client
        self.work_queue_url = work_queue_url

    @classmethod
    def from_app_state(cls, state: Any) -> "Dispatcher":
        """Construct from a FastAPI ``app.state`` (or any object with the
        same attributes). The lifespan handler populates ``state.publisher``,
        ``state.sqs_client``, ``state.settings``; this factory keeps
        background callers (replay loop in Phase 3, re-evaluation pass)
        out of the FastAPI ``Request`` dependency.
        """
        return cls(
            publisher=state.publisher,
            sqs_client=getattr(state, "sqs_client", None),
            work_queue_url=state.settings.work_queue_url,
        )

    async def persist_and_publish(
        self,
        session: AsyncSession,
        *,
        entity_type: str,
        action: str,
        payload: EventPayload,
        plan_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
    ) -> Event:
        """INSERT an Event row, flush, and publish it on the bus.

        Publish failures are logged and swallowed — the Event row is the
        source of truth. Callers that need to react to publish failures
        (e.g. the dispatcher, which writes a ``DispatchPublishFailed``
        marker per A.8) should use ``_persist_event`` + ``_publish``
        directly instead of this helper.
        """
        event = await self._persist_event(
            session,
            entity_type=entity_type, action=action, payload=payload,
            plan_id=plan_id, task_id=task_id, run_id=run_id, step_id=step_id,
        )
        try:
            await self.publisher.publish(event, payload)
        except Exception:
            logger.exception(
                "failed to publish %s.%s to events bus; continuing "
                "(Event row %s persisted)",
                entity_type, action, event.id,
            )
        return event

    async def _persist_event(
        self,
        session: AsyncSession,
        *,
        entity_type: str,
        action: str,
        payload: EventPayload,
        plan_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        commit_sha: str | None = None,
    ) -> Event:
        """INSERT + flush an Event row. No external publish. Internal seam
        for the dispatcher's per-target failure handling: persist first,
        then publish + (on failure) write a marker.

        ``commit_sha`` populates the per-ADR-0014 column on the Event row.
        The dispatcher's first-step ``step.ready`` path leaves it ``None``
        until ``head_sha`` resolution lands (A.3 follow-up); the
        cross-step dispatch path (B.2) stamps it from the prior step's
        envelope so ADR-0013's mergeability VIEW can LATERAL-join on it.
        """
        event = Event(
            entity_type=entity_type,
            action=action,
            plan_id=plan_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            payload=payload.model_dump(mode="json"),
            commit_sha=commit_sha,
        )
        session.add(event)
        await session.flush()
        return event

    async def _record_publish_failed(
        self,
        session: AsyncSession,
        *,
        original_event_id: uuid.UUID,
        target: str,
        error: BaseException,
        plan_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        run_id: uuid.UUID | None,
        step_id: uuid.UUID | None,
    ) -> None:
        """Persist a ``DispatchPublishFailed`` marker referencing the
        original event whose publish/send failed (A.8). Logged at WARNING
        with structured fields so ops alerting can fire on a sustained
        replay backlog. Errors emitting the marker itself are swallowed —
        a write storm against ``events`` should not crash the request."""
        payload = DispatchPublishFailed(
            original_event_id=original_event_id,
            target=target,  # type: ignore[arg-type]
            error_message=str(error)[:1024],
            attempted_at=datetime.now(timezone.utc),
        )
        logger.warning(
            "dispatch publish failed: target=%s original_event_id=%s error=%s",
            target, original_event_id, error,
            extra={
                "dispatch_publish_failed": True,
                "target": target,
                "original_event_id": str(original_event_id),
                "task_id": str(task_id) if task_id else None,
                "run_id": str(run_id) if run_id else None,
            },
        )
        try:
            await self._persist_event(
                session,
                entity_type="_internal",
                action="dispatch_publish_failed",
                payload=payload,
                plan_id=plan_id,
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
            )
        except Exception:
            # We can't recursively mark a failure-to-record-failure. Log
            # and move on — the original publish error is already in the
            # logs above.
            logger.exception(
                "failed to persist DispatchPublishFailed marker for "
                "original_event_id=%s; replay loop will not see this failure",
                original_event_id,
            )

    async def _has_step_ready_event(
        self, session: AsyncSession, task_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Idempotency probe for ``dispatch_task``: return the existing
        run_id if a ``step.ready`` event already exists for the task,
        else ``None``.

        The consumer's re-evaluation pass (D.6) calls ``dispatch_task``
        on tasks that may have become unblocked; a previously-dispatched
        task must short-circuit cleanly so re-entry is safe.
        """
        result = await session.execute(
            select(Event.run_id)
            .where(
                Event.task_id == task_id,
                Event.entity_type == "step",
                Event.action == "ready",
            )
            .order_by(Event.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_deferred_run(
        self, session: AsyncSession, task_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Find an existing ``WorkflowRun`` for the task that has not yet
        had ``step.ready`` emitted — the "deferred dispatch" case.

        The deferred-dispatch path (D.5 / D.2 gate failure) persists a
        ``WorkflowRun`` + step rows up front so the run graph stays
        complete, but skips publishing ``step.ready``. The caller — the
        consumer's re-evaluation pass (D.6) — wants to retry dispatch on
        these tasks when a satisfying event lands. Without this probe a
        re-call of ``dispatch_task`` would create a *duplicate* run; we
        return the existing run id so dispatch can reuse it and emit
        ``step.ready`` against its first step.

        Returns the oldest such run id (there should be exactly one in
        practice; ordering is defensive against future races).
        """
        result = await session.execute(
            select(WorkflowRun.id)
            .where(WorkflowRun.task_id == task_id)
            .order_by(WorkflowRun.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def dispatch_task(
        self, session: AsyncSession, task: Task
    ) -> uuid.UUID:
        """Create the WorkflowRun + step rows + step.ready event.

        Returns the run's id. The caller is responsible for committing
        the session — dispatch flushes but does not commit, so the run
        creation is part of the same transaction as the task creation
        that triggered it.

        Gating (in order):
          1. **Idempotency** — if a ``step.ready`` event already exists
             for this task, return the existing run_id without any
             further DB writes. Makes the consumer's re-evaluation pass
             (D.6) safe to call on already-dispatched tasks.
          2. **Deferred-run reuse** — if a ``WorkflowRun`` exists for
             this task but no ``step.ready`` event has been emitted
             (the D.2 / D.5 deferred-dispatch path on a prior call),
             reuse that run instead of creating a duplicate. The
             re-evaluation pass (D.6) calls back here after a satisfying
             event (``pr_merged``, ``plan.activated``, ...) lands; we
             must not stack a second run on top.
          3. **Plan-active gate (D.5)** — if the task's parent plan is
             not yet ``active`` (drafting / planning), persist the run +
             steps and return the run_id; skip publish + SQS send. The
             consumer's re-evaluation pass picks it up when
             ``plan.activated`` lands.
          4. **Dependency gate (D.2)** — if any ``task_dependencies``
             row is unsatisfied, same deferred-dispatch path: run + steps
             persisted, no publish + no send.
        """
        # ── 1. Idempotency probe ──────────────────────────────────────────
        existing_run_id = await self._has_step_ready_event(session, task.id)
        if existing_run_id is not None:
            logger.debug(
                "dispatch_task: task %s already dispatched (run %s); skipping",
                task.id, existing_run_id,
            )
            return existing_run_id

        # ── 2. Validate workflow steps exist before any side effects ─────
        result = await session.execute(
            select(WorkflowVersionStep)
            .where(
                WorkflowVersionStep.workflow_version_id == task.workflow_version_id
            )
            .order_by(WorkflowVersionStep.step_index)
        )
        wv_steps = list(result.scalars())
        if not wv_steps:
            raise DispatchError(
                f"workflow version {task.workflow_version_id} has no steps; "
                "cannot dispatch task"
            )

        wv = await session.get(WorkflowVersion, task.workflow_version_id)
        workflow_slug = wv.workflow_id if wv is not None else ""

        # ── 3. Reuse-or-create the WorkflowRun + step rows ───────────────
        # The run graph must exist in either dispatched or gated state — the
        # consumer's re-evaluation pass (D.6) reads ``task_status`` which
        # joins workflow_runs; a missing run would orphan the task.
        #
        # Hole 4 fix (2026-05-13): if a prior call deferred and left a run
        # behind, reuse it rather than stacking a duplicate. See
        # ``docs/handoffs/2026-05-13-ralph-loop-scoping-signal.md``.
        deferred_run_id = await self._find_deferred_run(session, task.id)
        if deferred_run_id is not None:
            run = await session.get(WorkflowRun, deferred_run_id)
            existing_steps_result = await session.execute(
                select(WorkflowRunStep)
                .where(WorkflowRunStep.run_id == deferred_run_id)
                .order_by(WorkflowRunStep.step_index)
            )
            run_steps = list(existing_steps_result.scalars())
            logger.info(
                "dispatch_task: reusing deferred run %s for task %s",
                deferred_run_id, task.id,
            )
        else:
            run = WorkflowRun(
                task_id=task.id,
                workflow_version_id=task.workflow_version_id,
                trigger="registered",
            )
            session.add(run)
            await session.flush()

            run_steps = []
            for wv_step in wv_steps:
                rs = WorkflowRunStep(
                    run_id=run.id,
                    step_index=wv_step.step_index,
                    step_name=wv_step.step_name,
                    role_id=wv_step.role_id,
                    status="pending",
                )
                session.add(rs)
                run_steps.append(rs)
            await session.flush()

        # ── 4. Plan-active gate (D.5) ─────────────────────────────────────
        # Read the ``plan_status`` VIEW; the flush above is critical so
        # Scenario 1 (PlanActivated emitted earlier in the same txn) is
        # visible here.
        if not await is_plan_active(session, task.plan_id):
            logger.info(
                "dispatch_task: deferring task %s — plan %s not active; "
                "persisted run %s pending re-evaluation",
                task.id, task.plan_id, run.id,
            )
            return run.id

        # ── 5. Dependency gate (D.2) ──────────────────────────────────────
        if not await evaluate_dependencies(session, task.id):
            logger.info(
                "dispatch_task: deferring task %s — dependencies unsatisfied; "
                "persisted run %s pending re-evaluation",
                task.id, run.id,
            )
            return run.id

        # ── 6. Publish + send ─────────────────────────────────────────────
        first_step = run_steps[0]

        payload = StepReady(
            role_id=first_step.role_id,
            step_index=first_step.step_index,
            step_name=first_step.step_name,
            repo=task.repo,
            workflow_id=workflow_slug,
        )

        # Persist the Event row before any external I/O so the source of
        # truth is durable even if the bus publish or queue send fails.
        ready_event = await self._persist_event(
            session,
            entity_type="step",
            action="ready",
            payload=payload,
            plan_id=task.plan_id,
            task_id=task.id,
            run_id=run.id,
            step_id=first_step.id,
        )
        try:
            await self.publisher.publish(ready_event, payload)
        except Exception as exc:
            logger.exception(
                "failed to publish step.ready to events bus; continuing "
                "(Event row %s persisted)",
                ready_event.id,
            )
            await self._record_publish_failed(
                session,
                original_event_id=ready_event.id,
                target="sns",
                error=exc,
                plan_id=task.plan_id,
                task_id=task.id,
                run_id=run.id,
                step_id=first_step.id,
            )

        if self.sqs_client is not None and self.work_queue_url is not None:
            try:
                # FIFO work queue requires MessageGroupId; group by run so
                # multiple steps in one run stay ordered. Per B.4 the claim
                # body carries all four IDs so the worker can publish
                # ``step.started`` before the API round-trip that fetches
                # the full context.
                await asyncio.to_thread(
                    self.sqs_client.send_message,
                    QueueUrl=self.work_queue_url,
                    MessageBody=json.dumps({
                        "step_id": str(first_step.id),
                        "task_id": str(task.id),
                        "plan_id": str(task.plan_id),
                        "run_id": str(run.id),
                    }),
                    MessageGroupId=str(run.id),
                )
            except Exception as exc:
                logger.exception(
                    "failed to send claim to work queue; worker will not pick up "
                    "this step until the replay loop reissues"
                )
                await self._record_publish_failed(
                    session,
                    original_event_id=ready_event.id,
                    target="sqs",
                    error=exc,
                    plan_id=task.plan_id,
                    task_id=task.id,
                    run_id=run.id,
                    step_id=first_step.id,
                )

        return run.id


def get_dispatcher(request: Request) -> Dispatcher:
    """FastAPI dependency: returns the request-scoped dispatcher.

    The dispatcher draws all clients from ``app.state`` (set by the
    lifespan handler), so a test can override these by mutating
    ``app.state`` before the request arrives.
    """
    return Dispatcher.from_app_state(request.app.state)
