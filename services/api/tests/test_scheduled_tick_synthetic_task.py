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

Coalesce-pending-ticks coverage (2026-06-03 6-tick wf-ui-triage backlog
follow-up) lives alongside: ``handle_scheduled_tick`` calls
``_coalesce_pending_ticks_for_schedule`` between the WorkflowVersion
lookup and the synthetic-task dispatch, and never for the two
deterministic-detector slugs (which short-circuit before any synthetic
task is built).
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

    def all(self) -> list[Any]:
        # The coalesce helper iterates over ``result.all()`` — wrap a
        # plain list so tests can stub the pending-tasks SELECT without
        # spinning up a real Row class.
        if self._value is None:
            return []
        if isinstance(self._value, list):
            return self._value
        return [self._value]


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
            _ExecResult(workflow_version),  # WorkflowVersion lookup
            _ExecResult([]),  # _coalesce_pending_ticks: no priors
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

    # task.registered emitted before dispatch_task.
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
        execute_results=[_ExecResult(None)],  # WorkflowVersion lookup returns None
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


# ── Coalesce pending ticks (2026-06-03 backlog follow-up) ────────────────────


@pytest.mark.asyncio
async def test_coalesces_prior_pending_tick() -> None:
    """A schedule with one pending prior tick gets the prior task
    cancelled with ``reason='superseded_by_newer_tick'`` + the schedule
    id, and the fresh tick still dispatches normally."""
    schedule_id = uuid.uuid4()
    schedule = MagicMock()
    schedule.id = schedule_id
    schedule.status = "active"
    schedule.workflow_id = "wf-ui-triage"

    workflow_version = MagicMock()
    workflow_version.id = uuid.uuid4()

    prior_task_id = uuid.uuid4()

    session = _StubAsyncSession(
        get_results=[schedule],
        execute_results=[
            _ExecResult(workflow_version),  # WorkflowVersion lookup
            _ExecResult([(prior_task_id,)]),  # one pending prior tick
        ],
    )

    expected_run_id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock(return_value=expected_run_id)

    typed = ScheduledTick(
        schedule_id=schedule_id,
        workflow_id="wf-ui-triage",
        rendered_payload={"repo": "acme/example"},
    )

    run_id = await triggers_mod.handle_scheduled_tick(
        session,  # type: ignore[arg-type]
        dispatcher,
        typed=typed,
    )

    # Fresh tick dispatched normally.
    assert run_id == expected_run_id
    dispatcher.dispatch_task.assert_awaited_once()

    # persist_and_publish was called twice: once for the prior task's
    # cancellation, then once for the fresh task's registration. Order
    # matters — cancellation must precede registration so an event-stream
    # consumer never sees two live ticks for the same schedule.
    assert dispatcher.persist_and_publish.await_count == 2
    cancel_call, register_call = dispatcher.persist_and_publish.await_args_list

    assert cancel_call.kwargs["entity_type"] == "task"
    assert cancel_call.kwargs["action"] == "cancelled"
    assert cancel_call.kwargs["task_id"] == prior_task_id
    assert cancel_call.kwargs["plan_id"] == SYSTEM_PLAN_ID
    cancel_payload = cancel_call.kwargs["payload"]
    assert isinstance(cancel_payload, TaskCancelled)
    assert cancel_payload.reason == "superseded_by_newer_tick"
    assert cancel_payload.schedule_id == schedule_id
    assert cancel_payload.cancelled_by == "scheduler-coalesce"

    assert register_call.kwargs["entity_type"] == "task"
    assert register_call.kwargs["action"] == "registered"
    assert register_call.kwargs["plan_id"] == SYSTEM_PLAN_ID


@pytest.mark.asyncio
async def test_does_not_coalesce_in_flight_tick() -> None:
    """When the SQL filter (``NOT EXISTS … started_at IS NOT NULL``)
    excludes the prior tick because a worker has already picked it up,
    the coalesce helper returns an empty list — no cancellation event
    fires and the fresh tick still dispatches in parallel."""
    schedule = MagicMock()
    schedule.id = uuid.uuid4()
    schedule.status = "active"
    schedule.workflow_id = "wf-ui-triage"

    workflow_version = MagicMock()
    workflow_version.id = uuid.uuid4()

    session = _StubAsyncSession(
        get_results=[schedule],
        execute_results=[
            _ExecResult(workflow_version),  # WorkflowVersion lookup
            _ExecResult([]),  # in-flight prior excluded by SQL → no rows
        ],
    )

    expected_run_id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock(return_value=expected_run_id)

    typed = ScheduledTick(
        schedule_id=schedule.id,
        workflow_id="wf-ui-triage",
        rendered_payload={"repo": "acme/example"},
    )

    run_id = await triggers_mod.handle_scheduled_tick(
        session,  # type: ignore[arg-type]
        dispatcher,
        typed=typed,
    )

    # Fresh tick dispatched normally — parallel runs are allowed.
    assert run_id == expected_run_id
    dispatcher.dispatch_task.assert_awaited_once()

    # persist_and_publish was called exactly once (task.registered only —
    # no cancellation because the in-flight prior was filtered out).
    assert dispatcher.persist_and_publish.await_count == 1
    only_call = dispatcher.persist_and_publish.await_args_list[0]
    assert only_call.kwargs["entity_type"] == "task"
    assert only_call.kwargs["action"] == "registered"


@pytest.mark.asyncio
async def test_stuck_task_sweep_never_coalesces() -> None:
    """The ``wf-stuck-task-sweep`` short-circuit at the top of
    ``handle_scheduled_tick`` runs a deterministic detector — no
    synthetic Task is created, so coalesce must not be invoked."""
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
        rendered_payload={"trigger": "scheduled-sweep"},
    )

    with (
        patch.object(
            sweep_mod, "run_stuck_task_sweep", new=AsyncMock(),
        ) as mocked_sweep,
        patch.object(
            triggers_mod,
            "_coalesce_pending_ticks_for_schedule",
            new=AsyncMock(),
        ) as mocked_coalesce,
        patch.object(
            triggers_mod,
            "_dispatch_via_synthetic_task",
            new=AsyncMock(),
        ) as mocked_dispatch,
    ):
        run_id = await triggers_mod.handle_scheduled_tick(
            session,  # type: ignore[arg-type]
            dispatcher,
            typed=typed,
        )

    assert run_id is None
    mocked_sweep.assert_awaited_once()
    mocked_coalesce.assert_not_awaited()
    mocked_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalation_close_sweep_never_coalesces() -> None:
    """Same invariant for the ADR-0062 escalation-close sweep slug —
    the short-circuit runs the deterministic close detector and never
    materializes a Task."""
    from treadmill_api.coordination import escalation_close_sweep as close_mod

    schedule = MagicMock()
    schedule.id = uuid.uuid4()
    schedule.status = "active"
    schedule.workflow_id = close_mod.ESCALATION_CLOSE_SWEEP_WORKFLOW_ID

    session = _StubAsyncSession(get_results=[schedule], execute_results=[])
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    dispatcher.dispatch_task = AsyncMock()

    typed = ScheduledTick(
        schedule_id=schedule.id,
        workflow_id=close_mod.ESCALATION_CLOSE_SWEEP_WORKFLOW_ID,
        rendered_payload={"trigger": "scheduled-sweep"},
    )

    with (
        patch.object(
            close_mod, "run_escalation_close_sweep", new=AsyncMock(),
        ) as mocked_close_sweep,
        patch.object(
            triggers_mod,
            "_coalesce_pending_ticks_for_schedule",
            new=AsyncMock(),
        ) as mocked_coalesce,
        patch.object(
            triggers_mod,
            "_dispatch_via_synthetic_task",
            new=AsyncMock(),
        ) as mocked_dispatch,
    ):
        run_id = await triggers_mod.handle_scheduled_tick(
            session,  # type: ignore[arg-type]
            dispatcher,
            typed=typed,
        )

    assert run_id is None
    mocked_close_sweep.assert_awaited_once()
    mocked_coalesce.assert_not_awaited()
    mocked_dispatch.assert_not_awaited()
