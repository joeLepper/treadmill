"""Tests for ``handle_scheduled_tick`` — coalesce-pending-ticks path.

Covers the ``_coalesce_pending_ticks_for_schedule`` helper and its call
site in ``handle_scheduled_tick`` (2026-06-03 6-tick wf-ui-triage backlog
follow-up).  Non-coalesce invariants (synthetic-task creation, paused/missing
cases) live in ``test_scheduled_tick_synthetic_task.py``.
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
        if self._value is None:
            return []
        if isinstance(self._value, list):
            return self._value
        return [self._value]


class _StubAsyncSession:
    """Async session double that returns a different result per call.

    Calls are taken in order: ``execute_results.pop(0)``.
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
        if not getattr(entity, "id", None):
            entity.id = uuid.uuid4()

    async def flush(self) -> None:
        self.flushed += 1


# ── Coalesce pending ticks ────────────────────────────────────────────────────


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

    # persist_and_publish called twice: cancellation then registration.
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
    """When the SQL filter excludes the prior tick because a worker has
    already picked it up, no cancellation fires and the fresh tick
    dispatches in parallel (parallel runs are allowed)."""
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

    assert run_id == expected_run_id
    dispatcher.dispatch_task.assert_awaited_once()

    # Only task.registered — no cancellation.
    assert dispatcher.persist_and_publish.await_count == 1
    only_call = dispatcher.persist_and_publish.await_args_list[0]
    assert only_call.kwargs["entity_type"] == "task"
    assert only_call.kwargs["action"] == "registered"


@pytest.mark.asyncio
async def test_stuck_task_sweep_never_coalesces() -> None:
    """The ``wf-stuck-task-sweep`` short-circuit runs a deterministic
    detector — no synthetic Task is created, so coalesce must not be
    invoked."""
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
    """The ADR-0062 escalation-close sweep short-circuit runs the
    deterministic close detector and never materializes a Task."""
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
