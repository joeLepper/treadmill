"""Unit tests for the synthetic-task dispatch path in ``handle_scheduled_tick``.

Per ADR-0057, a scheduled tick for any non-intercept workflow (i.e. NOT
``wf-stuck-task-sweep``) now creates a synthetic ``Task`` row tied to the
system Plan and dispatches via the normal task-bound ``dispatch_task`` path
instead of the deprecated ``_create_and_publish_run_without_task``.

These tests mock the DB session and the dispatcher to verify the correct
ORM objects are constructed and the right dispatcher method is called.

Validation:
  ``cd services/api && uv run pytest tests/test_scheduled_tick_synthetic_task.py -q``
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination.triggers import handle_scheduled_tick
from treadmill_api.events.schedule import ScheduledTick
from treadmill_api.models import Task
from treadmill_api.seed.system_plan import SYSTEM_PLAN_ID


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_schedule(
    workflow_id: str = "wf-tune-judge-prompts",
    status: str = "active",
) -> MagicMock:
    s = MagicMock()
    s.status = status
    s.workflow_id = workflow_id
    return s


def _make_wv() -> MagicMock:
    wv = MagicMock()
    wv.id = uuid.uuid4()
    return wv


def _make_session(
    schedule: object = None,
    wv: object = None,
    system_plan: object = True,  # truthy = plan present
) -> AsyncMock:
    """Async session mock returning schedule + wv + system Plan appropriately."""
    session = AsyncMock()

    # session.get: returns schedule for schedule_id, Plan for SYSTEM_PLAN_ID
    async def _get(model, pk):
        # Distinguish by model class name to avoid importing inside the mock.
        if getattr(model, "__name__", "") == "Plan":
            return MagicMock() if system_plan else None
        return schedule  # default: return the schedule

    session.get.side_effect = _get

    # session.execute: returns a result whose scalar_one_or_none() is wv
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = wv
    session.execute.return_value = mock_result

    return session


def _make_dispatcher(run_id: uuid.UUID | None = None) -> AsyncMock:
    d = AsyncMock()
    d.dispatch_task.return_value = run_id or uuid.uuid4()
    return d


def _make_tick(
    workflow_id: str = "wf-tune-judge-prompts",
    repo: str = "testorg/testrepo",
    trigger: str = "scheduled-tune",
) -> ScheduledTick:
    return ScheduledTick(
        schedule_id=uuid.uuid4(),
        workflow_id=workflow_id,
        rendered_payload={"trigger": trigger, "repo": repo},
    )


# ── happy path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_scheduled_tick_creates_task_row() -> None:
    """A Task object is session.add'd for a non-intercept schedule."""
    schedule = _make_schedule()
    wv = _make_wv()
    session = _make_session(schedule=schedule, wv=wv)
    dispatcher = _make_dispatcher()
    typed = _make_tick()

    await handle_scheduled_tick(session, dispatcher, typed=typed)

    added_objects = [c.args[0] for c in session.add.call_args_list]
    tasks = [o for o in added_objects if isinstance(o, Task)]
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_handle_scheduled_tick_task_plan_id() -> None:
    session = _make_session(schedule=_make_schedule(), wv=_make_wv())
    dispatcher = _make_dispatcher()

    await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    added = [c.args[0] for c in session.add.call_args_list]
    task = next(o for o in added if isinstance(o, Task))
    assert task.plan_id == SYSTEM_PLAN_ID


@pytest.mark.asyncio
async def test_handle_scheduled_tick_task_repo_from_payload() -> None:
    session = _make_session(schedule=_make_schedule(), wv=_make_wv())
    dispatcher = _make_dispatcher()

    await handle_scheduled_tick(
        session, dispatcher,
        typed=_make_tick(repo="owner/custom-repo"),
    )

    added = [c.args[0] for c in session.add.call_args_list]
    task = next(o for o in added if isinstance(o, Task))
    assert task.repo == "owner/custom-repo"


@pytest.mark.asyncio
async def test_handle_scheduled_tick_task_created_by_scheduler() -> None:
    session = _make_session(schedule=_make_schedule(), wv=_make_wv())
    dispatcher = _make_dispatcher()

    await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    added = [c.args[0] for c in session.add.call_args_list]
    task = next(o for o in added if isinstance(o, Task))
    assert task.created_by == "scheduler"


@pytest.mark.asyncio
async def test_handle_scheduled_tick_task_title_contains_trigger_and_workflow() -> None:
    session = _make_session(schedule=_make_schedule(), wv=_make_wv())
    dispatcher = _make_dispatcher()

    await handle_scheduled_tick(
        session, dispatcher,
        typed=_make_tick(
            workflow_id="wf-tune-judge-prompts",
            trigger="scheduled-tune",
        ),
    )

    added = [c.args[0] for c in session.add.call_args_list]
    task = next(o for o in added if isinstance(o, Task))
    assert "scheduled-tune" in task.title
    assert "wf-tune-judge-prompts" in task.title


@pytest.mark.asyncio
async def test_handle_scheduled_tick_calls_dispatch_task() -> None:
    """``dispatcher.dispatch_task`` must be called with the synthetic task."""
    session = _make_session(schedule=_make_schedule(), wv=_make_wv())
    dispatcher = _make_dispatcher()

    await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    dispatcher.dispatch_task.assert_called_once()
    _, called_task = dispatcher.dispatch_task.call_args.args
    assert isinstance(called_task, Task)


@pytest.mark.asyncio
async def test_handle_scheduled_tick_returns_run_id() -> None:
    expected_run_id = uuid.uuid4()
    session = _make_session(schedule=_make_schedule(), wv=_make_wv())
    dispatcher = _make_dispatcher(run_id=expected_run_id)

    result = await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    assert result == expected_run_id


@pytest.mark.asyncio
async def test_handle_scheduled_tick_flushes_before_dispatch() -> None:
    """session.flush must be called so the Task row is visible to dispatch_task."""
    session = _make_session(schedule=_make_schedule(), wv=_make_wv())
    dispatcher = _make_dispatcher()

    await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    session.flush.assert_called()


# ── skip / guard conditions ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_scheduled_tick_skips_when_schedule_not_found() -> None:
    session = _make_session(schedule=None, wv=_make_wv())
    dispatcher = _make_dispatcher()

    result = await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    assert result is None
    dispatcher.dispatch_task.assert_not_called()


@pytest.mark.asyncio
async def test_handle_scheduled_tick_skips_when_schedule_paused() -> None:
    session = _make_session(schedule=_make_schedule(status="paused"), wv=_make_wv())
    dispatcher = _make_dispatcher()

    result = await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    assert result is None
    dispatcher.dispatch_task.assert_not_called()


@pytest.mark.asyncio
async def test_handle_scheduled_tick_skips_when_no_workflow_version() -> None:
    session = _make_session(schedule=_make_schedule(), wv=None)
    dispatcher = _make_dispatcher()

    result = await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    assert result is None
    dispatcher.dispatch_task.assert_not_called()


@pytest.mark.asyncio
async def test_handle_scheduled_tick_skips_when_system_plan_missing() -> None:
    """Returns None when the system Plan has not been seeded yet."""
    session = _make_session(
        schedule=_make_schedule(), wv=_make_wv(), system_plan=False
    )
    dispatcher = _make_dispatcher()

    result = await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    assert result is None
    dispatcher.dispatch_task.assert_not_called()


@pytest.mark.asyncio
async def test_handle_scheduled_tick_stuck_sweep_uses_deterministic_detector() -> None:
    """``wf-stuck-task-sweep`` runs the deterministic detector, not dispatch_task."""
    session = _make_session(
        schedule=_make_schedule(workflow_id="wf-stuck-task-sweep"),
        wv=_make_wv(),
    )
    dispatcher = _make_dispatcher()

    with patch(
        "treadmill_api.coordination.stuck_task_sweep.run_stuck_task_sweep",
        new_callable=AsyncMock,
    ) as mock_sweep:
        result = await handle_scheduled_tick(
            session, dispatcher,
            typed=_make_tick(workflow_id="wf-stuck-task-sweep"),
        )

    assert result is None
    mock_sweep.assert_called_once()
    dispatcher.dispatch_task.assert_not_called()


@pytest.mark.asyncio
async def test_handle_scheduled_tick_dispatch_error_returns_none() -> None:
    """A ``DispatchError`` from ``dispatch_task`` is caught → returns None."""
    from treadmill_api.dispatch import DispatchError

    session = _make_session(schedule=_make_schedule(), wv=_make_wv())
    dispatcher = _make_dispatcher()
    dispatcher.dispatch_task.side_effect = DispatchError("no steps")

    result = await handle_scheduled_tick(session, dispatcher, typed=_make_tick())

    assert result is None
