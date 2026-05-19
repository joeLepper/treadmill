"""Tests for the arch-cap-reached operator notification surface (ADR-0048 §3).

When wf-architecture-resolve hits its per-task dispatch cap (5, per ADR-0029
Q29.e), the system must emit a ``task.escalated_to_operator`` event so the
operator can intervene. Previously the cap silently returned None.

Tests are unit-level (no live Postgres) — sessions and dispatcher are stubbed
with AsyncMock / MagicMock.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.triggers import (
    FEEDBACK_WORKFLOW_ID,
    _emit_arch_cap_reached,
    maybe_dispatch_arbitration_on_deadlock,
)
from treadmill_api.events.step import StepCompleted
from treadmill_api.events.step_output import Metadata, StepOutput
from treadmill_api.events.task import TaskEscalatedToOperator


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_dispatcher() -> MagicMock:
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()
    return dispatcher


def _make_task(task_id: uuid.UUID, repo: str = "test/repo") -> MagicMock:
    task = MagicMock()
    task.id = task_id
    task.repo = repo
    task.plan_id = uuid.uuid4()
    return task


def _make_step_output_row(verdict: str, reasoning: str) -> MagicMock:
    row = MagicMock()
    row.output = {"payload": {"verdict": verdict, "reasoning": reasoning}}
    return row


# ── Direct tests for _emit_arch_cap_reached ───────────────────────────────────


@pytest.mark.asyncio
async def test_emit_calls_persist_and_publish_once() -> None:
    """_emit_arch_cap_reached must call dispatcher.persist_and_publish once
    when the task row exists and DB queries succeed."""
    task_id = uuid.uuid4()
    repo = "org/repo"
    run_id = uuid.uuid4()
    task = _make_task(task_id, repo)
    dispatcher = _make_dispatcher()

    session = AsyncMock()
    session.get = AsyncMock(return_value=task)

    r1 = MagicMock()
    r1.first = MagicMock(return_value=_make_step_output_row("amend", "Plan was wrong."))
    r2 = MagicMock()
    r2.scalars = MagicMock(return_value=[run_id])
    session.execute = AsyncMock(side_effect=[r1, r2])

    await _emit_arch_cap_reached(session, dispatcher, task_id=task_id, repo=repo)

    assert dispatcher.persist_and_publish.await_count == 1
    kw = dispatcher.persist_and_publish.await_args.kwargs
    assert kw["entity_type"] == "task"
    assert kw["action"] == "escalated_to_operator"
    assert kw["task_id"] == task_id
    assert kw["plan_id"] == task.plan_id


@pytest.mark.asyncio
async def test_emit_payload_carries_task_id_verdict_reasoning() -> None:
    """The TaskEscalatedToOperator payload must carry the load-bearing fields:
    task_id, last_verdict, and last_reasoning."""
    task_id = uuid.uuid4()
    repo = "org/repo"
    run_id = uuid.uuid4()
    task = _make_task(task_id, repo)
    dispatcher = _make_dispatcher()

    session = AsyncMock()
    session.get = AsyncMock(return_value=task)

    r1 = MagicMock()
    r1.first = MagicMock(
        return_value=_make_step_output_row("accept-as-is", "Code is correct per the ADR.")
    )
    r2 = MagicMock()
    r2.scalars = MagicMock(return_value=[run_id])
    session.execute = AsyncMock(side_effect=[r1, r2])

    await _emit_arch_cap_reached(session, dispatcher, task_id=task_id, repo=repo)

    payload = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    assert isinstance(payload, TaskEscalatedToOperator)
    assert payload.task_id == task_id
    assert payload.repo == repo
    assert payload.last_verdict == "accept-as-is"
    assert payload.last_reasoning == "Code is correct per the ADR."
    assert str(run_id) in payload.run_ids


@pytest.mark.asyncio
async def test_emit_skips_when_dispatcher_is_none() -> None:
    """If dispatcher is None (unit-test stub), emit silently returns without
    querying the DB."""
    session = AsyncMock()

    await _emit_arch_cap_reached(
        session, None, task_id=uuid.uuid4(), repo="org/repo"
    )

    assert session.get.await_count == 0
    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_emit_skips_when_task_not_found() -> None:
    """If session.get returns None, emit logs and returns without calling
    persist_and_publish."""
    dispatcher = _make_dispatcher()
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    await _emit_arch_cap_reached(
        session, dispatcher, task_id=uuid.uuid4(), repo="org/repo"
    )

    assert dispatcher.persist_and_publish.await_count == 0


@pytest.mark.asyncio
async def test_emit_payload_when_no_prior_steps() -> None:
    """When no prior architect step exists, the payload fires with
    last_verdict=None and last_reasoning=None (nulls, not crash)."""
    task_id = uuid.uuid4()
    task = _make_task(task_id)
    dispatcher = _make_dispatcher()

    session = AsyncMock()
    session.get = AsyncMock(return_value=task)

    r1 = MagicMock()
    r1.first = MagicMock(return_value=None)
    r2 = MagicMock()
    r2.scalars = MagicMock(return_value=[])
    session.execute = AsyncMock(side_effect=[r1, r2])

    await _emit_arch_cap_reached(session, dispatcher, task_id=task_id, repo="org/repo")

    payload = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    assert isinstance(payload, TaskEscalatedToOperator)
    assert payload.last_verdict is None
    assert payload.last_reasoning is None
    assert payload.run_ids == []


# ── End-to-end: cap block in maybe_dispatch_arbitration_on_deadlock ───────────


@pytest.mark.asyncio
async def test_arbitration_trigger_fires_event_when_capped(monkeypatch) -> None:
    """When _is_capped returns True for wf-architecture-resolve, the deadlock
    trigger must call _emit_arch_cap_reached with the correct task_id + repo.

    This is the ADR-0048 §3 escalation-3 surface: automated recovery is
    exhausted and the operator must intervene."""
    task_id = uuid.uuid4()
    repo = "org/repo"

    typed = StepCompleted(
        completed_at="2026-05-19T12:00:00+00:00",
        output=StepOutput(
            summary="no change",
            decision="responded-without-change",
            payload={},
            metadata=Metadata(),
        ),
    )

    wf_row = MagicMock()
    wf_row.workflow_id = FEEDBACK_WORKFLOW_ID
    wf_row.run_id = uuid.uuid4()
    wf_row.task_id = task_id
    wf_row.repo = repo
    r_wf = MagicMock()
    r_wf.first = MagicMock(return_value=wf_row)

    gate_row = MagicMock()
    gate_row.output = {"decision": "changes_requested"}
    r_gate = MagicMock()
    r_gate.first = MagicMock(return_value=gate_row)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[r_wf, r_gate])

    import treadmill_api.coordination.triggers as triggers_mod

    monkeypatch.setattr(triggers_mod, "_is_capped", AsyncMock(return_value=True))
    emit_mock = AsyncMock()
    monkeypatch.setattr(triggers_mod, "_emit_arch_cap_reached", emit_mock)

    result = await maybe_dispatch_arbitration_on_deadlock(
        session,
        _make_dispatcher(),
        step_id=str(uuid.uuid4()),
        typed=typed,
    )

    assert result is None, "capped trigger must return None (no workflow dispatch)"
    assert emit_mock.await_count == 1, (
        "must call _emit_arch_cap_reached exactly once when the arch cap fires"
    )
    kw = emit_mock.await_args.kwargs
    assert kw["task_id"] == task_id
    assert kw["repo"] == repo


@pytest.mark.asyncio
async def test_arbitration_trigger_no_event_when_not_capped(monkeypatch) -> None:
    """When _is_capped returns False, _emit_arch_cap_reached must NOT be
    called — the normal dispatch path proceeds without emitting the event."""
    task_id = uuid.uuid4()
    repo = "org/repo"

    typed = StepCompleted(
        completed_at="2026-05-19T12:00:00+00:00",
        output=StepOutput(
            summary="no change",
            decision="responded-without-change",
            payload={},
            metadata=Metadata(),
        ),
    )

    wf_row = MagicMock()
    wf_row.workflow_id = FEEDBACK_WORKFLOW_ID
    wf_row.run_id = uuid.uuid4()
    wf_row.task_id = task_id
    wf_row.repo = repo
    r_wf = MagicMock()
    r_wf.first = MagicMock(return_value=wf_row)

    gate_row = MagicMock()
    gate_row.output = {"decision": "changes_requested"}
    r_gate = MagicMock()
    r_gate.first = MagicMock(return_value=gate_row)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[r_wf, r_gate])
    # Task lookup returns None → dispatch short-circuits without needing
    # further mock setup for the full dispatch path.
    session.get = AsyncMock(return_value=None)

    import treadmill_api.coordination.triggers as triggers_mod

    monkeypatch.setattr(triggers_mod, "_is_capped", AsyncMock(return_value=False))
    emit_mock = AsyncMock()
    monkeypatch.setattr(triggers_mod, "_emit_arch_cap_reached", emit_mock)

    await maybe_dispatch_arbitration_on_deadlock(
        session,
        _make_dispatcher(),
        step_id=str(uuid.uuid4()),
        typed=typed,
    )

    assert emit_mock.await_count == 0, (
        "_emit_arch_cap_reached must not be called when the cap is not reached"
    )
