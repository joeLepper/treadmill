"""Unit tests for the terminal-step-failure escalation producer (ADR-0062 Step 1).

Drives ``maybe_dispatch_terminal_step_failure_escalation`` directly with
stubs so we can prove the three branching behaviors without a live
Postgres:

* Happy path: a ``step.failed`` lands on a run with no remaining
  pending steps and no concurrent cap-reached escalation → emit
  ``task.escalated_to_operator`` with the expected ``reason``,
  ``step_name``, and ``gate_log_excerpt``.
* Dedup: a recent ``task.escalated_to_operator`` event already covers
  the task → the producer short-circuits without emitting.
* Pending steps remain: the cross-step loop will advance the run →
  the producer short-circuits silently.

Pattern mirrors ``test_gate_broken_trigger.py``: the session is a
MagicMock whose ``execute`` is an AsyncMock with a ``side_effect``
queue, so each consecutive trigger query reads a stubbed result.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.triggers import (
    maybe_dispatch_terminal_step_failure_escalation,
)
from treadmill_api.events.task import TaskEscalatedToOperator


# ── Helpers ──────────────────────────────────────────────────────────────────


def _scalar_one_result(value: Any) -> Any:
    """Shim for a SQLAlchemy result whose ``.scalar_one()`` returns ``value``."""
    res = MagicMock()
    res.scalar_one = MagicMock(return_value=value)
    return res


def _first_result(row: Any) -> Any:
    """Shim for a SQLAlchemy result whose ``.first()`` returns ``row``."""
    res = MagicMock()
    res.first = MagicMock(return_value=row)
    return res


def _step_row(
    *,
    step_id: uuid.UUID,
    run_id: uuid.UUID,
    step_index: int,
    step_name: str,
    step_error: str | None,
    task_id: uuid.UUID,
    repo: str,
    plan_id: uuid.UUID,
) -> Any:
    """Mock the (step + task) joined row the producer reads."""
    row = MagicMock()
    row.step_id = step_id
    row.run_id = run_id
    row.step_index = step_index
    row.step_name = step_name
    row.step_error = step_error
    row.task_id = task_id
    row.repo = repo
    row.plan_id = plan_id
    return row


# ── Happy path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_step_failure_happy_path_emits_escalation() -> None:
    """A ``step.failed`` reaches a terminal workflow state with no
    concurrent cap-reached escalation → escalation fires once with
    ``reason='terminal_step_failure'``, ``step_name``, and the step's
    captured error as ``gate_log_excerpt``."""
    step_id = uuid.uuid4()
    run_id = uuid.uuid4()
    task_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _first_result(_step_row(
            step_id=step_id,
            run_id=run_id,
            step_index=2,
            step_name="action",
            step_error="Traceback (most recent call last): KeyError 'verdict'",
            task_id=task_id,
            repo="joeLepper/treadmill",
            plan_id=plan_id,
        )),
        _scalar_one_result(0),  # no remaining pending steps
        _first_result(None),    # no recent escalation in dedup window
    ])

    event = MagicMock()
    event.id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock(return_value=event)

    result = await maybe_dispatch_terminal_step_failure_escalation(
        session, dispatcher, step_id=str(step_id),
    )

    assert result == event.id
    dispatcher.persist_and_publish.assert_awaited_once()
    kwargs = dispatcher.persist_and_publish.await_args.kwargs
    assert kwargs["entity_type"] == "task"
    assert kwargs["action"] == "escalated_to_operator"
    assert kwargs["plan_id"] == plan_id
    assert kwargs["task_id"] == task_id

    payload: TaskEscalatedToOperator = kwargs["payload"]
    assert isinstance(payload, TaskEscalatedToOperator)
    assert payload.task_id == task_id
    assert payload.repo == "joeLepper/treadmill"
    assert payload.reason == "terminal_step_failure"
    assert payload.step_name == "action"
    assert payload.gate_log_excerpt is not None
    assert "KeyError" in payload.gate_log_excerpt
    # The failing run's id is surfaced for operator reference.
    assert payload.run_ids == [str(run_id)]


@pytest.mark.asyncio
async def test_terminal_step_failure_emits_with_none_excerpt_when_no_error_text() -> None:
    """A step row with an empty / NULL ``error`` column still fires the
    escalation — the absence of a captured excerpt isn't a skip
    condition; only the dedup + pending-steps gates short-circuit."""
    step_id = uuid.uuid4()
    run_id = uuid.uuid4()
    task_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _first_result(_step_row(
            step_id=step_id,
            run_id=run_id,
            step_index=0,
            step_name="analyze",
            step_error=None,
            task_id=task_id,
            repo="joeLepper/treadmill",
            plan_id=plan_id,
        )),
        _scalar_one_result(0),
        _first_result(None),
    ])

    event = MagicMock()
    event.id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock(return_value=event)

    result = await maybe_dispatch_terminal_step_failure_escalation(
        session, dispatcher, step_id=str(step_id),
    )

    assert result == event.id
    payload: TaskEscalatedToOperator = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    assert payload.reason == "terminal_step_failure"
    assert payload.step_name == "analyze"
    assert payload.gate_log_excerpt is None


# ── Dedup: prior escalation within window short-circuits ─────────────────────


@pytest.mark.asyncio
async def test_terminal_step_failure_skips_when_recent_escalation_exists() -> None:
    """A wf-conflict-cap-reached escalation fired 30s earlier (within the
    dedup window) → the terminal-step-failure producer skips because the
    cap-reached producer has already covered this case."""
    step_id = uuid.uuid4()
    run_id = uuid.uuid4()
    task_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _first_result(_step_row(
            step_id=step_id,
            run_id=run_id,
            step_index=0,
            step_name="action",
            step_error="boom",
            task_id=task_id,
            repo="joeLepper/treadmill",
            plan_id=plan_id,
        )),
        _scalar_one_result(0),
        # A prior escalation row exists within the dedup window.
        _first_result(MagicMock(id=uuid.uuid4())),
    ])

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    result = await maybe_dispatch_terminal_step_failure_escalation(
        session, dispatcher, step_id=str(step_id),
    )

    assert result is None
    dispatcher.persist_and_publish.assert_not_awaited()


# ── Workflow has more steps queued → skip ────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_step_failure_skips_when_pending_steps_remain() -> None:
    """A failing step whose run still has at least one pending step past
    it → the cross-step loop will advance; the producer skips so the
    next terminal owns the escalation decision."""
    step_id = uuid.uuid4()
    run_id = uuid.uuid4()
    task_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _first_result(_step_row(
            step_id=step_id,
            run_id=run_id,
            step_index=0,
            step_name="analyze",
            step_error="boom",
            task_id=task_id,
            repo="joeLepper/treadmill",
            plan_id=plan_id,
        )),
        _scalar_one_result(1),  # one pending step past this one — loop will advance
    ])

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    result = await maybe_dispatch_terminal_step_failure_escalation(
        session, dispatcher, step_id=str(step_id),
    )

    assert result is None
    dispatcher.persist_and_publish.assert_not_awaited()
    # The dedup query was never issued — predicate short-circuits earlier.
    assert session.execute.await_count == 2


# ── Defensive skip cases ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_step_failure_skips_when_dispatcher_absent() -> None:
    """No dispatcher → no escalation (test-stub safety, mirrors the rest
    of the trigger module)."""
    session = MagicMock()
    session.execute = AsyncMock()

    result = await maybe_dispatch_terminal_step_failure_escalation(
        session, dispatcher=None, step_id=str(uuid.uuid4()),
    )

    assert result is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_step_failure_skips_when_step_unresolvable() -> None:
    """The owning step + task can't be resolved → skip silently. Covers
    the race where the step row was deleted between dispatch and the
    terminal projection."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_first_result(None)])

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    result = await maybe_dispatch_terminal_step_failure_escalation(
        session, dispatcher, step_id=str(uuid.uuid4()),
    )

    assert result is None
    dispatcher.persist_and_publish.assert_not_awaited()
