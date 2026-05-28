"""Unit tests for the gate-broken-on-architect-verdict trigger (ADR-0058).

Drives ``maybe_dispatch_gate_broken_escalation`` directly with stubs so
we can prove the trigger's short-circuit behavior + the happy-path
side-effect (emit ``task.escalated_to_operator`` with
``reason='gate-broken'`` + the gate's stderr) without needing live
Postgres. Pattern mirrors ``test_supersede_trigger.py``.

Live-DB coverage (the full chain through ``CoordinationConsumer.handle``
hitting Postgres) belongs alongside the other architect-verdict
integration tests behind ``TREADMILL_INTEGRATION=1``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.triggers import (
    ARCHITECTURE_RESOLVE_WORKFLOW_ID,
    maybe_dispatch_gate_broken_escalation,
)
from treadmill_api.events.step import StepCompleted
from treadmill_api.events.step_output import Metadata, StepOutput


# ── Helpers ──────────────────────────────────────────────────────────────────


_VALID_EXCERPT = (
    "--- stderr ---\n"
    "Traceback (most recent call last):\n"
    "  File \"/var/treadmill/workspaces/.../repo/app.py\", line 3, in <module>\n"
    "    import aws_cdk\n"
    "ModuleNotFoundError: No module named 'aws_cdk'\n"
)


def _step_completed_with(
    verdict: str,
    *,
    gate_log_excerpt: str | None = None,
    reasoning: str | None = "the gate is broken",
) -> StepCompleted:
    """Build a ``StepCompleted`` with the architect's payload shape."""
    payload: dict[str, Any] = {
        "verdict": verdict,
        "reasoning": reasoning,
        "target_artifact": "tasks/<id>/validation",
    }
    if gate_log_excerpt is not None:
        payload["gate_log_excerpt"] = gate_log_excerpt
    return StepCompleted(
        completed_at="2026-05-28T15:00:00+00:00",
        output=StepOutput(
            summary="architect output",
            decision=verdict,
            commit_sha=None,
            artifacts=[],
            payload=payload,
            metadata=Metadata(),
        ),
    )


def _result(value: Any) -> Any:
    """Build a SQLAlchemy ``execute``-result shim."""
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)

    class _Scalars:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def __iter__(self) -> Any:
            return iter(self._rows)

    res.scalars = MagicMock(return_value=_Scalars(value if isinstance(value, list) else []))
    return res


# ── Short-circuit tests (no DB work on non-gate-broken payloads) ─────────────


@pytest.mark.asyncio
async def test_gate_broken_helper_short_circuits_on_amend_verdict() -> None:
    """Non-gate-broken verdict bails before any DB query."""
    session = MagicMock()
    session.execute = AsyncMock()  # would raise if called
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    result = await maybe_dispatch_gate_broken_escalation(
        session,
        dispatcher,
        step_id=str(uuid.uuid4()),
        typed=_step_completed_with("amend"),
    )

    assert result is None
    session.execute.assert_not_awaited()
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_broken_helper_short_circuits_on_missing_excerpt() -> None:
    """``verdict='gate-broken'`` but no ``gate_log_excerpt`` bails fast —
    the worker-side parser + the API-side validator both should have
    blocked this earlier, but the trigger defends rather than emitting
    an empty-excerpt escalation."""
    session = MagicMock()
    session.execute = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    result = await maybe_dispatch_gate_broken_escalation(
        session,
        dispatcher,
        step_id=str(uuid.uuid4()),
        typed=_step_completed_with("gate-broken", gate_log_excerpt=None),
    )

    assert result is None
    session.execute.assert_not_awaited()
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_broken_helper_short_circuits_on_whitespace_excerpt() -> None:
    """Whitespace-only excerpt is the same as missing — diagnostic value
    is zero; refuse to emit."""
    session = MagicMock()
    session.execute = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    result = await maybe_dispatch_gate_broken_escalation(
        session,
        dispatcher,
        step_id=str(uuid.uuid4()),
        typed=_step_completed_with("gate-broken", gate_log_excerpt="   \n\t  "),
    )

    assert result is None
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_broken_helper_short_circuits_on_non_architect_workflow() -> None:
    """A gate-broken-shaped payload from a non-architect step is ignored
    — only ``wf-architecture-resolve`` steps can produce gate-broken
    escalations."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _result("wf-feedback"),  # WorkflowVersion lookup returns a wrong workflow
    ])
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    result = await maybe_dispatch_gate_broken_escalation(
        session,
        dispatcher,
        step_id=str(uuid.uuid4()),
        typed=_step_completed_with("gate-broken", gate_log_excerpt=_VALID_EXCERPT),
    )

    assert result is None
    # The workflow-id lookup happened, but the task-fetch did not.
    assert session.execute.await_count == 1
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_broken_helper_skips_when_dispatcher_absent() -> None:
    """No dispatcher → no escalation (matches the existing
    ``_emit_arch_cap_reached`` / ``_emit_operator_escalation`` shape)."""
    session = MagicMock()
    session.execute = AsyncMock()

    result = await maybe_dispatch_gate_broken_escalation(
        session,
        dispatcher=None,
        step_id=str(uuid.uuid4()),
        typed=_step_completed_with("gate-broken", gate_log_excerpt=_VALID_EXCERPT),
    )

    assert result is None
    session.execute.assert_not_awaited()


# ── Happy-path: emit escalation with reason + excerpt ────────────────────────


@pytest.mark.asyncio
async def test_gate_broken_helper_emits_escalation_with_reason_and_excerpt() -> None:
    """Valid gate-broken verdict → persist_and_publish fires with a
    ``TaskEscalatedToOperator`` payload carrying ``reason='gate-broken'``
    + the architect's ``gate_log_excerpt`` (truncated to 4000 chars)."""
    from treadmill_api.events.task import TaskEscalatedToOperator

    task_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    task = MagicMock()
    task.id = task_id
    task.plan_id = plan_id
    task.repo = "joeLepper/treadmill"

    run_id_1, run_id_2 = uuid.uuid4(), uuid.uuid4()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _result(ARCHITECTURE_RESOLVE_WORKFLOW_ID),  # workflow_id lookup
        _result(task),                              # task lookup
        _result([run_id_1, run_id_2]),              # recent run ids
    ])

    event = MagicMock()
    event.id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock(return_value=event)

    result = await maybe_dispatch_gate_broken_escalation(
        session,
        dispatcher,
        step_id=str(uuid.uuid4()),
        typed=_step_completed_with(
            "gate-broken",
            gate_log_excerpt=_VALID_EXCERPT,
            reasoning=(
                "Trigger B (ralph-loop deadlock): the worker sandbox is "
                "missing aws-cdk-lib"
            ),
        ),
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
    assert payload.reason == "gate-broken"
    assert payload.last_verdict == "gate-broken"
    assert "ralph-loop deadlock" in (payload.last_reasoning or "")
    assert payload.gate_log_excerpt is not None
    assert "ModuleNotFoundError" in payload.gate_log_excerpt
    # Recent run IDs surfaced for operator reference.
    assert len(payload.run_ids) == 2


@pytest.mark.asyncio
async def test_gate_broken_helper_caps_excerpt_at_4000_chars() -> None:
    """Excerpts longer than 4000 chars are truncated before the
    ``TaskEscalatedToOperator`` validator can reject them (validator is
    capped at 4000); preserves the leading 4000 chars."""
    from treadmill_api.events.task import TaskEscalatedToOperator

    huge = "x" * 5000  # would fail the Pydantic max_length=4000 directly
    task = MagicMock()
    task.id = uuid.uuid4()
    task.plan_id = uuid.uuid4()
    task.repo = "joeLepper/treadmill"

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _result(ARCHITECTURE_RESOLVE_WORKFLOW_ID),
        _result(task),
        _result([uuid.uuid4()]),
    ])

    event = MagicMock()
    event.id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock(return_value=event)

    result = await maybe_dispatch_gate_broken_escalation(
        session,
        dispatcher,
        step_id=str(uuid.uuid4()),
        typed=_step_completed_with("gate-broken", gate_log_excerpt=huge),
    )

    assert result == event.id
    payload: TaskEscalatedToOperator = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    assert len(payload.gate_log_excerpt or "") == 4000
