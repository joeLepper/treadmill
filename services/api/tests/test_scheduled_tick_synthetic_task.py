"""Unit tests for ``handle_scheduled_tick`` synthetic-task path (ADR-0057).

The pre-fix tick called ``_create_and_publish_run_without_task`` which
sent ``task_id=null`` to workers; workers silently died inside
``_handle_step``. The fix dispatches a **synthetic Task** tied to
``SYSTEM_PLAN_ID`` via the normal ``dispatcher.dispatch_task`` path —
workers see a normal task body, every existing test of dispatch_task
covers this surface too.

These tests verify the new surface invariants on top of the stub
dispatcher: the synthetic Task is created with ``plan_id == SYSTEM_PLAN_ID``,
``persist_and_publish`` is called with ``task.registered`` first, then
``dispatch_task`` is called with the new Task. We do NOT exercise the
``_create_and_publish_run_without_task`` taskless path — it's deprecated
and only retained for the 4 historical orphan runs.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination import triggers as triggers_mod
from treadmill_api.events.schedule import ScheduledTick
from treadmill_api.events.task import TaskCancelled
from treadmill_api.seed.system_plan import SYSTEM_PLAN_ID


# ── Stub session ──────────────────────────────────────────────────────────────


class _ExecResult:
    """Mimics SQLAlchemy ``Result``: one value, returned on demand."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        assert self._value is not None
        return self._value

    def scalars(self) -> list[Any]:
        return [] if self._value is None else [self._value]


class _StubAsyncSession:
    """Async session double that returns a different result per call.

    Calls are taken in order: ``execute_results.pop(0)``. Use
    ``get_results`` to enqueue ``session.get`` returns.
    """

    def __init__(
        self,
        *,
        get_results: list[Any],
        execute_results: list[Any],
    ) -> None:
        self._get_results = list(get_results)
        self._execute_results = list(execute_results)
        self.added: list[Any] = []
        self.flushed = 0

    async def get(self, _model: Any, _key: Any) -> Any:
        return self._get_results.pop(0)

    async def execute(self, *args: Any, **kwargs: Any) -> _ExecResult:
        return self._execute_results.pop(0)

    def add(self, entity: Any) -> None:
        self.added.append(entity)
        # Give Task / WorkflowRun rows an id at flush time — caller code
        # reads task.id after add(). Generated lazily so each row gets a
        # distinct id.
        if not getattr(entity, "id", None):
            entity.id = uuid.uuid4()

    async def flush(self) -> None:
        self.flushed += 1


# ── 200/201: happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_tick_creates_synthetic_task_and_dispatches() -> None:
    """The fix's load-bearing invariant: a tick on an active schedule with
    a valid ``repo`` in rendered_payload creates a Task tied to
    SYSTEM_PLAN_ID and dispatches via ``dispatcher.dispatch_task``."""
    schedule = MagicMock()
    schedule.status = "active"
    schedule.workflow_id = "wf-tune-judge-prompts"

    workflow_version = MagicMock()
    workflow_version.id = uuid.uuid4()

    session = _StubAsyncSession(
        get_results=[schedule],  # the Schedule lookup
        execute_results=[
            # 1) WorkflowVersion lookup in handle_scheduled_tick (used to
            #    parameterize the pending-tick coalesce probe).
            _ExecResult(workflow_version),
            # 2) Coalesce-find SELECT: no prior pending ticks for this
            #    schedule, so scalars() yields the empty list.
            _ExecResult(None),
            # 3) WorkflowVersion lookup inside _dispatch_via_synthetic_task
            #    (the dispatch helper resolves the WV again to stay
            #    callable from the operator-trigger router without sharing
            #    a pre-resolved object).
            _ExecResult(workflow_version),
        ],
    )

    expected_run_id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock(return_value=expected_run_id)

    typed = ScheduledTick(
        schedule_id=uuid.uuid4(),
        workflow_id="wf-tune-judge-prompts",
        rendered_payload={"repo": "acme/example", "trigger": "scheduled-tune"},
    )

    run_id = await triggers_mod.handle_scheduled_tick(
        session,  # type: ignore[arg-type]
        dispatcher,
        typed=typed,
    )

    assert run_id == expected_run_id

    # A Task row was added with the canonical system Plan as parent.
    tasks = [a for a in session.added if hasattr(a, "plan_id")]
    assert len(tasks) == 1, f"expected 1 Task, got {len(tasks)}: {session.added}"
    task = tasks[0]
    assert task.plan_id == SYSTEM_PLAN_ID
    assert task.repo == "acme/example"
    assert task.workflow_version_id == workflow_version.id
    assert task.created_by == "scheduler"

    # task.registered emitted once before dispatch_task; no task.cancelled
    # since the coalesce probe found nothing.
    dispatcher.persist_and_publish.assert_awaited_once()
    pp_kwargs = dispatcher.persist_and_publish.await_args.kwargs
    assert pp_kwargs["entity_type"] == "task"
    assert pp_kwargs["action"] == "registered"
    assert pp_kwargs["plan_id"] == SYSTEM_PLAN_ID
    assert pp_kwargs["task_id"] == task.id

    # dispatch_task called with the synthetic Task.
    dispatcher.dispatch_task.assert_awaited_once()
    dt_args = dispatcher.dispatch_task.await_args.args
    assert dt_args[1] is task


# ── Skip: schedule paused ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_tick_skips_paused_schedule_without_creating_task() -> None:
    """A paused schedule must not produce a Task (was a bug class in the
    old taskless path that would still publish a step.ready)."""
    schedule = MagicMock()
    schedule.status = "paused"
    schedule.workflow_id = "wf-tune-judge-prompts"

    session = _StubAsyncSession(get_results=[schedule], execute_results=[])
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock()

    typed = ScheduledTick(
        schedule_id=uuid.uuid4(),
        workflow_id="wf-tune-judge-prompts",
        rendered_payload={"repo": "acme/example"},
    )

    run_id = await triggers_mod.handle_scheduled_tick(
        session,  # type: ignore[arg-type]
        dispatcher,
        typed=typed,
    )

    assert run_id is None
    assert session.added == []
    dispatcher.persist_and_publish.assert_not_awaited()
    dispatcher.dispatch_task.assert_not_awaited()


# ── Skip: payload missing repo ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_tick_skips_when_payload_missing_repo() -> None:
    """The synthetic Task needs a ``repo`` (Task.repo is NOT NULL). The
    old code silently propagated an empty repo into step.ready; the new
    code logs a warning and skips."""
    schedule = MagicMock()
    schedule.status = "active"
    schedule.workflow_id = "wf-tune-judge-prompts"

    session = _StubAsyncSession(get_results=[schedule], execute_results=[])
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock()

    typed = ScheduledTick(
        schedule_id=uuid.uuid4(),
        workflow_id="wf-tune-judge-prompts",
        rendered_payload={},  # no 'repo'
    )

    run_id = await triggers_mod.handle_scheduled_tick(
        session,  # type: ignore[arg-type]
        dispatcher,
        typed=typed,
    )

    assert run_id is None
    assert session.added == []
    dispatcher.persist_and_publish.assert_not_awaited()
    dispatcher.dispatch_task.assert_not_awaited()


# ── Skip: no WorkflowVersion ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_tick_skips_when_workflow_version_missing() -> None:
    """An un-seeded install can't materialize the run — the helper
    short-circuits before adding any Task row."""
    schedule = MagicMock()
    schedule.status = "active"
    schedule.workflow_id = "wf-not-yet-seeded"

    session = _StubAsyncSession(
        get_results=[schedule],
        execute_results=[
            # 1) WorkflowVersion lookup in handle_scheduled_tick returns
            #    None → coalesce probe is skipped uniformly.
            _ExecResult(None),
            # 2) _dispatch_via_synthetic_task's own WorkflowVersion lookup
            #    also returns None and surfaces the warn-and-skip.
            _ExecResult(None),
        ],
    )
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock()

    typed = ScheduledTick(
        schedule_id=uuid.uuid4(),
        workflow_id="wf-not-yet-seeded",
        rendered_payload={"repo": "acme/example"},
    )

    run_id = await triggers_mod.handle_scheduled_tick(
        session,  # type: ignore[arg-type]
        dispatcher,
        typed=typed,
    )

    assert run_id is None
    assert session.added == []
    dispatcher.persist_and_publish.assert_not_awaited()
    dispatcher.dispatch_task.assert_not_awaited()


# ── Coalesce: prior pending tick gets superseded ─────────────────────────────


@pytest.mark.asyncio
async def test_coalesces_prior_pending_tick() -> None:
    """The 2026-06-03 wf-ui-triage backlog: a schedule whose start
    latency exceeds the cron interval accumulates pending ticks. The
    coalesce helper emits ``task.cancelled`` for the prior pending
    before the new tick dispatches, so the operator surface sees the
    newest tick, not a stack."""
    schedule_id = uuid.uuid4()
    schedule = MagicMock()
    schedule.id = schedule_id
    schedule.status = "active"
    schedule.workflow_id = "wf-ui-triage"

    workflow_version = MagicMock()
    workflow_version.id = uuid.uuid4()

    prior_pending_task_id = uuid.uuid4()

    session = _StubAsyncSession(
        get_results=[schedule],
        execute_results=[
            # 1) WV lookup in handle_scheduled_tick.
            _ExecResult(workflow_version),
            # 2) Coalesce-find query: one prior pending tick task matches
            #    (no started step, no terminal lifecycle event).
            _ExecResult(prior_pending_task_id),
            # 3) WV lookup inside _dispatch_via_synthetic_task.
            _ExecResult(workflow_version),
        ],
    )

    expected_run_id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock(return_value=expected_run_id)

    typed = ScheduledTick(
        schedule_id=schedule_id,
        workflow_id="wf-ui-triage",
        rendered_payload={"repo": "joeLepper/treadmill"},
    )

    run_id = await triggers_mod.handle_scheduled_tick(
        session,  # type: ignore[arg-type]
        dispatcher,
        typed=typed,
    )

    # New tick still dispatches normally.
    assert run_id == expected_run_id
    dispatcher.dispatch_task.assert_awaited_once()

    # persist_and_publish fired twice: once for the prior pending's
    # task.cancelled (coalesce), once for the new synthetic task's
    # task.registered (dispatch).
    assert dispatcher.persist_and_publish.await_count == 2
    cancel_call, register_call = dispatcher.persist_and_publish.await_args_list

    # Order matters: coalesce runs BEFORE dispatch so the prior is
    # off-surface before the new tick lands.
    assert cancel_call.kwargs["action"] == "cancelled"
    assert cancel_call.kwargs["entity_type"] == "task"
    assert cancel_call.kwargs["task_id"] == prior_pending_task_id
    assert cancel_call.kwargs["plan_id"] == SYSTEM_PLAN_ID
    cancel_payload = cancel_call.kwargs["payload"]
    assert isinstance(cancel_payload, TaskCancelled)
    assert cancel_payload.reason == "superseded_by_newer_tick"
    assert cancel_payload.schedule_id == schedule_id
    assert cancel_payload.cancelled_by == "scheduler-coalesce"

    assert register_call.kwargs["action"] == "registered"
    assert register_call.kwargs["entity_type"] == "task"


# ── Coalesce: in-flight tick is preserved ────────────────────────────────────


@pytest.mark.asyncio
async def test_does_not_coalesce_in_flight_tick() -> None:
    """A prior tick whose step has begun execution (``started_at`` set
    on a workflow_run_steps row) is NOT cancelled — parallel runs of
    the same schedule are allowed by design; we only collapse the
    pending queue. The query's ``NOT EXISTS started_at`` predicate
    filters the in-flight task out, so the coalesce-find returns
    nothing and the new tick dispatches alongside the in-flight one."""
    schedule_id = uuid.uuid4()
    schedule = MagicMock()
    schedule.id = schedule_id
    schedule.status = "active"
    schedule.workflow_id = "wf-ui-triage"

    workflow_version = MagicMock()
    workflow_version.id = uuid.uuid4()

    session = _StubAsyncSession(
        get_results=[schedule],
        execute_results=[
            # 1) WV lookup in handle_scheduled_tick.
            _ExecResult(workflow_version),
            # 2) Coalesce-find: the prior tick's step has started_at set,
            #    so the NOT EXISTS clause excludes it; query returns 0
            #    rows (None on the stub → empty scalars).
            _ExecResult(None),
            # 3) WV lookup inside _dispatch_via_synthetic_task.
            _ExecResult(workflow_version),
        ],
    )

    expected_run_id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock(return_value=expected_run_id)

    typed = ScheduledTick(
        schedule_id=schedule_id,
        workflow_id="wf-ui-triage",
        rendered_payload={"repo": "joeLepper/treadmill"},
    )

    run_id = await triggers_mod.handle_scheduled_tick(
        session,  # type: ignore[arg-type]
        dispatcher,
        typed=typed,
    )

    # New tick dispatched.
    assert run_id == expected_run_id
    dispatcher.dispatch_task.assert_awaited_once()

    # Only one persist_and_publish call: the new task.registered. No
    # task.cancelled because the in-flight tick is preserved.
    assert dispatcher.persist_and_publish.await_count == 1
    only_call = dispatcher.persist_and_publish.await_args
    assert only_call.kwargs["action"] == "registered"


# ── Sweep short-circuits never reach the coalesce helper ─────────────────────


@pytest.mark.asyncio
async def test_stuck_task_sweep_never_coalesces() -> None:
    """``wf-stuck-task-sweep`` runs deterministic queries in-band — no
    synthetic Task is created, so there's no pending-tick stack to
    coalesce. The short-circuit must fire BEFORE the coalesce helper."""
    from treadmill_api.coordination import stuck_task_sweep as sweep_mod

    schedule = MagicMock()
    schedule.id = uuid.uuid4()
    schedule.status = "active"
    schedule.workflow_id = sweep_mod.STUCK_TASK_SWEEP_WORKFLOW_ID

    session = _StubAsyncSession(get_results=[schedule], execute_results=[])
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock()

    typed = ScheduledTick(
        schedule_id=schedule.id,
        workflow_id=sweep_mod.STUCK_TASK_SWEEP_WORKFLOW_ID,
        rendered_payload={},
    )

    with (
        patch.object(
            sweep_mod, "run_stuck_task_sweep", new=AsyncMock(return_value=0),
        ) as mocked_sweep,
        patch.object(
            triggers_mod,
            "_coalesce_pending_ticks_for_schedule",
            new=AsyncMock(),
        ) as mocked_coalesce,
    ):
        run_id = await triggers_mod.handle_scheduled_tick(
            session,  # type: ignore[arg-type]
            dispatcher,
            typed=typed,
        )

    assert run_id is None
    mocked_sweep.assert_awaited_once()
    mocked_coalesce.assert_not_awaited()
    # No synthetic task created either.
    assert session.added == []
    dispatcher.dispatch_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalation_close_sweep_never_coalesces() -> None:
    """Same shape as the stuck-task sweep: ``wf-escalation-close-sweep``
    runs a deterministic detector in-band, no synthetic Task, nothing
    to coalesce. Short-circuit fires before the helper."""
    from treadmill_api.coordination import (
        escalation_close_sweep as close_sweep_mod,
    )

    schedule = MagicMock()
    schedule.id = uuid.uuid4()
    schedule.status = "active"
    schedule.workflow_id = close_sweep_mod.ESCALATION_CLOSE_SWEEP_WORKFLOW_ID

    session = _StubAsyncSession(get_results=[schedule], execute_results=[])
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock()

    typed = ScheduledTick(
        schedule_id=schedule.id,
        workflow_id=close_sweep_mod.ESCALATION_CLOSE_SWEEP_WORKFLOW_ID,
        rendered_payload={},
    )

    with (
        patch.object(
            close_sweep_mod,
            "run_escalation_close_sweep",
            new=AsyncMock(return_value=0),
        ) as mocked_sweep,
        patch.object(
            triggers_mod,
            "_coalesce_pending_ticks_for_schedule",
            new=AsyncMock(),
        ) as mocked_coalesce,
    ):
        run_id = await triggers_mod.handle_scheduled_tick(
            session,  # type: ignore[arg-type]
            dispatcher,
            typed=typed,
        )

    assert run_id is None
    mocked_sweep.assert_awaited_once()
    mocked_coalesce.assert_not_awaited()
    assert session.added == []
    dispatcher.dispatch_task.assert_not_awaited()
