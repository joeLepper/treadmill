"""End-to-end smoke test for the gate-broken architect verdict (ADR-0058 Step 6).

Step 3 (PR #53) shipped the unit-level trigger coverage in
``test_gate_broken_trigger.py`` (short-circuits + happy path + the
4000-char cap). This file closes the integration gap: it drives the
real ``CoordinationConsumer._maybe_dispatch_gate_broken_escalation``
seam — the method ``consumer.handle`` calls on every
``step.completed`` — against a stubbed session and dispatcher and
proves the routing lands a ``task.escalated_to_operator`` event with
``reason='gate-broken'``, the architect's stderr excerpt, and the
recent architect run ids.

Pattern mirrors the supersede unit-test pattern
(``test_supersede_trigger.py::test_consumer_routes_step_completed_to_supersede_helper``):
no live Postgres, no real dispatcher, no SQS, no AWS. Pure-shape
assertion that the consumer-side glue routes a real-shaped
architect verdict to the trigger and the trigger emits the right
event payload — fast (<1s), sandbox-hermetic, deterministic.

Live-DB coverage (full chain via ``CoordinationConsumer.handle``
against Postgres) belongs alongside the other architect-verdict
integration tests behind ``TREADMILL_INTEGRATION=1`` and is not the
scope of this smoke test.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.consumer import CoordinationConsumer
from treadmill_api.coordination.triggers import ARCHITECTURE_RESOLVE_WORKFLOW_ID
from treadmill_api.events.step import StepCompleted
from treadmill_api.events.step_output import Metadata, StepOutput
from treadmill_api.events.task import TaskEscalatedToOperator


# ── Stubs ────────────────────────────────────────────────────────────────────


class _StubSession:
    """Minimal AsyncSession-ish stub for the consumer-routing tests."""

    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.commit = AsyncMock()
        self.flush = AsyncMock()


def _stub_factory(session: _StubSession) -> Any:
    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    def _make() -> Any:
        return _cm()

    return _make


def _result(value: Any) -> Any:
    """Build a SQLAlchemy ``execute``-result shim.

    Mirrors ``test_gate_broken_trigger.py::_result`` so the trigger's
    ``scalar_one_or_none`` / ``scalars`` access patterns work against
    the same shape used at the unit layer.
    """
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)

    class _Scalars:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def __iter__(self) -> Any:
            return iter(self._rows)

    res.scalars = MagicMock(
        return_value=_Scalars(value if isinstance(value, list) else [])
    )
    return res


_VALID_EXCERPT = (
    "--- stderr ---\n"
    "Traceback (most recent call last):\n"
    "  File \"/var/treadmill/workspaces/.../repo/app.py\", line 3, in <module>\n"
    "    import aws_cdk\n"
    "ModuleNotFoundError: No module named 'aws_cdk'\n"
)


# ── Smoke test ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_architect_gate_broken_verdict_emits_escalation_event() -> None:
    """End-to-end smoke: consumer routes a real-shaped architect
    ``gate-broken`` ``step.completed`` to the trigger, and the trigger
    emits a ``task.escalated_to_operator`` event carrying
    ``reason='gate-broken'`` + the gate's stderr excerpt + the
    architect's recent run ids.

    Drives ``CoordinationConsumer._maybe_dispatch_gate_broken_escalation``
    directly so the proof is the consumer-side wiring (the method
    ``consumer.handle`` calls on every ``step.completed``) + the
    downstream trigger producing the right payload. No Postgres, no
    real dispatcher — pure shape assertion.
    """
    # ── Arrange: real-shaped architect StepCompleted ─────────────────────
    typed = StepCompleted(
        completed_at="2026-05-28T15:00:00+00:00",
        output=StepOutput(
            summary="architect verdict: gate-broken",
            decision="gate-broken",
            commit_sha=None,
            artifacts=[],
            payload={
                "verdict": "gate-broken",
                "reasoning": (
                    "Trigger B (ralph-loop deadlock): the worker sandbox "
                    "is missing aws-cdk-lib; the validate gate cannot "
                    "import the app and there is nothing the author can "
                    "do from inside the loop."
                ),
                "target_artifact": "tasks/<id>/validation",
                "gate_log_excerpt": _VALID_EXCERPT,
            },
            metadata=Metadata(),
        ),
    )

    # ── Arrange: stub session returning a real-shaped Task row ───────────
    task_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    repo = "joeLepper/treadmill"
    task = MagicMock()
    task.id = task_id
    task.plan_id = plan_id
    task.repo = repo

    run_id_1, run_id_2 = uuid.uuid4(), uuid.uuid4()

    session = _StubSession()
    # Order matches the trigger's three execute() calls:
    #   1. workflow_id lookup (via WorkflowVersion join)  → wf-architecture-resolve
    #   2. task lookup (via WorkflowRun.task_id join)     → the Task row
    #   3. recent architect run ids                       → [run_id_1, run_id_2]
    session.execute = AsyncMock(side_effect=[
        _result(ARCHITECTURE_RESOLVE_WORKFLOW_ID),
        _result(task),
        _result([run_id_1, run_id_2]),
    ])

    # ── Arrange: stub dispatcher that records persist_and_publish kwargs ─
    event = MagicMock()
    event.id = uuid.uuid4()
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock(return_value=event)

    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(session),  # type: ignore[arg-type]
        dispatcher=dispatcher,
    )

    step_id = str(uuid.uuid4())

    # ── Act: drive the consumer's gate-broken seam directly ──────────────
    await consumer._maybe_dispatch_gate_broken_escalation(
        session,  # type: ignore[arg-type]
        step_id,
        typed,
    )

    # ── Assert: dispatcher fired exactly once with the right shape ───────
    dispatcher.persist_and_publish.assert_awaited_once()
    kwargs = dispatcher.persist_and_publish.await_args.kwargs
    assert kwargs["entity_type"] == "task"
    assert kwargs["action"] == "escalated_to_operator"
    assert kwargs["plan_id"] == plan_id
    assert kwargs["task_id"] == task_id

    payload = kwargs["payload"]
    assert isinstance(payload, TaskEscalatedToOperator)
    assert payload.task_id == task_id
    assert payload.repo == repo
    assert payload.reason == "gate-broken"
    assert payload.last_verdict == "gate-broken"
    assert payload.last_reasoning is not None
    assert "ralph-loop deadlock" in payload.last_reasoning
    # Architect's stderr excerpt is carried verbatim onto the event
    # so the operator sees the actual tooling failure without re-
    # running the loop.
    assert payload.gate_log_excerpt is not None
    assert "ModuleNotFoundError" in payload.gate_log_excerpt
    assert "No module named 'aws_cdk'" in payload.gate_log_excerpt
    # Recent architect run ids surfaced for operator reference.
    assert len(payload.run_ids) == 2

    # Three execute() calls fired in order: workflow-id, task, run-ids.
    assert session.execute.await_count == 3
