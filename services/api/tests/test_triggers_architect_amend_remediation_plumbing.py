"""Unit tests for the architect-amend → wf-feedback trigger plumbing.

The architect's ``amend`` verdict carries a ``remediation_summary`` —
the verbatim file:line directive the wf-feedback analyzer must honor.
Historically that directive was dropped at the dispatch boundary
(``maybe_dispatch_feedback_on_architect_amend`` ignored upstream
payload) and the analyzer re-evaluated the original failure signal
from scratch, often re-concluding ``no code change needed`` against
the architect's explicit directive (PRs #120/#122/#123/#124).

This module pins the structured-FK plumbing fix per ADR-0048 + ADR-0011:
the architect's step_id is passed through as ``source_step_id`` on the
new ``wf-feedback`` ``WorkflowRun`` row. The steps router joins through
that FK on context fetch and surfaces the upstream step's output (which
lives in ``workflow_run_steps.output`` — the one JSONB column the
architecture commits to per ADR-0011) as a ``source_step`` block on the
worker context. No new JSONB column on ``workflow_runs``.

The cap-policy + dedup + non-amend short-circuit branches are covered
by the existing consumer-routing + cap tests; this module focuses on
the plumbing assertion.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import treadmill_api.coordination.triggers as triggers
from treadmill_api.coordination.triggers import (
    maybe_dispatch_feedback_on_architect_amend,
)
from treadmill_api.events.step import StepCompleted
from treadmill_api.events.step_output import Metadata, StepOutput


def _amend_payload() -> StepCompleted:
    """A representative architect ``amend`` step.completed payload —
    the verdict the analyzer must honor verbatim."""
    return StepCompleted(
        completed_at="2026-05-19T12:00:00+00:00",
        output=StepOutput(
            summary="architect amend verdict",
            decision="amend",
            payload={
                "verdict": "amend",
                "remediation_summary": (
                    "Edit services/api/treadmill_api/foo.py to add "
                    "a None-guard at line 42."
                ),
                "reasoning": (
                    "The intent is correct; the code drops None at "
                    "the entrance. Add the guard inline."
                ),
                "dispatch": {"workflow_id": "wf-plan"},
            },
            metadata=Metadata(),
        ),
    )


def _stub_session_for_amend(
    *,
    architect_step_id: uuid.UUID,
    architect_run_id: uuid.UUID,
    task_id: uuid.UUID,
    repo: str = "owner/repo",
) -> Any:
    """Wire a session stub so the amend trigger's lookup chain
    resolves to a ``wf-architecture-resolve`` row + a Task row,
    without touching Postgres.

    The trigger executes three queries in order:
      1. ``select(...).join(WorkflowRunStep, WorkflowVersion, Task)``
         joined by step_id — returns ``(workflow_id, run_id, task_id,
         repo)``. We return a wf-architecture-resolve row.
      2. ``_is_capped(...)`` — runs a count query. We return 0 (not
         capped).
      3. ``session.get(Task, task_id)`` — returns a Task-shaped stub.
    """
    session = MagicMock()
    # First execute: the architect-step join. ``first()`` returns the
    # tuple shape the trigger destructures.
    join_row = MagicMock()
    join_row.workflow_id = triggers.ARCHITECTURE_RESOLVE_WORKFLOW_ID
    join_row.run_id = architect_run_id
    join_row.task_id = task_id
    join_row.repo = repo
    join_result = MagicMock()
    join_result.first = MagicMock(return_value=join_row)
    # Second execute (via _is_capped): a count scalar. Return 0 (uncapped).
    count_result = MagicMock()
    count_result.scalar_one = MagicMock(return_value=0)
    session.execute = AsyncMock(side_effect=[join_result, count_result])
    # session.get → Task row (the helper's third lookup before dispatch).
    task = MagicMock(id=task_id, repo=repo, plan_id=uuid.uuid4())
    session.get = AsyncMock(return_value=task)
    # The dedup helper short-circuits via session.execute when it
    # records a dedup row; we patch maybe_dispatch_with_dedup entirely
    # in the test below so this stub stays simple.
    return session


@pytest.mark.asyncio
async def test_architect_amend_dispatch_passes_source_step_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plumbing assertion: when the architect verdicts ``amend``,
    the helper calls ``_create_and_publish_run`` with
    ``source_step_id`` set to the architect's step_id (so the
    downstream wf-feedback worker can read the architect's
    ``remediation_summary`` from ``workflow_run_steps.output`` on its
    initial step.context fetch).

    Per ADR-0011: no new JSONB column — the plumbing is a structured
    UUID FK pointing at the upstream step row; the JSONB payload lives
    on ``workflow_run_steps.output``, the one column the architecture
    commits to JSONB.
    """
    architect_step_id = uuid.uuid4()
    architect_run_id = uuid.uuid4()
    task_id = uuid.uuid4()
    session = _stub_session_for_amend(
        architect_step_id=architect_step_id,
        architect_run_id=architect_run_id,
        task_id=task_id,
    )

    # Capture the kwargs the trigger passes through to the run creator.
    captured: dict[str, Any] = {}
    dispatched_run_id = uuid.uuid4()

    async def _capture_run(
        _session: Any,
        _dispatcher: Any,
        **kwargs: Any,
    ) -> uuid.UUID:
        captured.update(kwargs)
        return dispatched_run_id

    monkeypatch.setattr(triggers, "_create_and_publish_run", _capture_run)

    # The dedup wrapper would normally hash + insert a dedup row; bypass
    # it by passing through directly to dispatch_fn so the assertion is
    # focused on the plumbing.
    async def _passthrough_dedup(
        _session: Any,
        *,
        workflow_id: str,
        payload: dict[str, Any],
        dispatch_fn: Any,
    ) -> uuid.UUID | None:
        return await dispatch_fn()

    monkeypatch.setattr(
        triggers, "maybe_dispatch_with_dedup", _passthrough_dedup,
    )

    result = await maybe_dispatch_feedback_on_architect_amend(
        session,
        dispatcher=None,
        step_id=str(architect_step_id),
        typed=_amend_payload(),
    )

    assert result == dispatched_run_id
    assert captured["workflow_id"] == triggers.FEEDBACK_WORKFLOW_ID
    assert captured["trigger"] == "self:architect-amend"
    assert "source_step_id" in captured, (
        "amend trigger MUST pass the architect's step_id as "
        "source_step_id so the downstream wf-feedback worker can read "
        "the architect's remediation_summary on its initial context "
        "fetch — without this plumbing the directive is dropped at the "
        "dispatch boundary (observed on PRs #120/#122/#123/#124)"
    )
    # The trigger receives ``step_id`` as a str; the run creator
    # normalizes to UUID at the model boundary. Either shape is
    # acceptable here — what matters is that the architect's step is
    # the value carried through.
    passed = captured["source_step_id"]
    assert str(passed) == str(architect_step_id), (
        "source_step_id must be the architect's step_id (the step "
        "whose output carries remediation_summary), not the task_id "
        "or run_id"
    )


@pytest.mark.asyncio
async def test_architect_amend_short_circuits_on_non_amend_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the trigger must not invoke
    ``_create_and_publish_run`` (and therefore not write a
    ``source_step_id``) for non-amend verdicts. Other verdicts
    (``supersede``, ``accept-as-is``) route through different
    triggers; the amend path must short-circuit cleanly."""
    session = MagicMock()
    session.execute = AsyncMock()

    called = False

    async def _should_not_run(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        return uuid.uuid4()

    monkeypatch.setattr(triggers, "_create_and_publish_run", _should_not_run)

    typed = StepCompleted(
        completed_at="2026-05-19T12:00:00+00:00",
        output=StepOutput(
            summary="architect supersede verdict",
            decision="supersede",
            payload={"verdict": "supersede"},
            metadata=Metadata(),
        ),
    )

    result = await maybe_dispatch_feedback_on_architect_amend(
        session,
        dispatcher=None,
        step_id=str(uuid.uuid4()),
        typed=typed,
    )

    assert result is None
    assert called is False, (
        "amend trigger must short-circuit before _create_and_publish_run "
        "on non-amend verdicts"
    )
    assert session.execute.await_count == 0, (
        "non-amend verdicts must short-circuit before any DB query"
    )
