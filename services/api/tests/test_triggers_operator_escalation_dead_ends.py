"""Unit tests for the operator-escalation backstop (2026-05-19 dead-end audit).

Covers:
  * ``maybe_escalate_operator_on_terminal_give_up`` — wf-ci-fix / wf-conflict /
    wf-doc-amend completing with decision='fail' emit
    ``task.escalated_to_operator`` (SDE-2/4b/6).
  * ``_emit_operator_escalation`` — generic emitter: dispatcher-None and
    missing-task no-ops, payload shape, and the (task, signal) dedup guard.

Pure unit tests with mocked session/dispatcher — no DB, no live API.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.triggers import (
    _emit_operator_escalation,
    maybe_escalate_operator_on_terminal_give_up,
)
from treadmill_api.events.step import StepCompleted
from treadmill_api.events.step_output import Metadata, StepOutput


def _make_typed(decision: str = "fail") -> StepCompleted:
    return StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(
            summary="terminal",
            decision=decision,
            commit_sha=None,
            artifacts=[],
            payload={},
            metadata=Metadata(),
        ),
    )


def _make_row(workflow_id: str, task_id: uuid.UUID, repo: str = "example/repo") -> MagicMock:
    row = MagicMock()
    row.workflow_id = workflow_id
    row.task_id = task_id
    row.repo = repo
    return row


def _fake_task(task_id: uuid.UUID, repo: str = "example/repo") -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.repo = repo
    t.plan_id = uuid.uuid4()
    return t


def _make_session(
    *,
    step_row: Any,
    existing_escalation: Any = None,
    task: Any = None,
) -> AsyncMock:
    """Stub the give-up path's queries in order:
      1. step lookup → result.first() = step_row
      2. (inside _emit_operator_escalation) dedup lookup → result.first() = existing
    plus ``session.get(Task, ...)`` = task.
    """
    session = AsyncMock()
    r_step = MagicMock()
    r_step.first.return_value = step_row
    r_dedup = MagicMock()
    r_dedup.first.return_value = existing_escalation
    session.execute = AsyncMock(side_effect=[r_step, r_dedup])
    session.get = AsyncMock(return_value=task)
    return session


def _escalation_calls(dispatcher: MagicMock) -> list[dict[str, Any]]:
    return [
        kw
        for (_args, kw) in dispatcher.persist_and_publish.await_args_list
        if kw.get("action") == "escalated_to_operator"
    ]


# ── give-up trigger ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "workflow_id,expected_signal",
    [
        ("wf-ci-fix", "wf-ci-fix-gave-up"),
        ("wf-conflict", "wf-conflict-gave-up"),
        ("wf-doc-amend", "wf-doc-amend-gave-up"),
    ],
)
@pytest.mark.asyncio
async def test_give_up_decision_fail_escalates(workflow_id: str, expected_signal: str) -> None:
    task_id = uuid.uuid4()
    task = _fake_task(task_id)
    session = _make_session(step_row=_make_row(workflow_id, task_id), task=task)
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await maybe_escalate_operator_on_terminal_give_up(
        session, dispatcher, step_id=str(uuid.uuid4()), typed=_make_typed("fail"),
    )

    calls = _escalation_calls(dispatcher)
    assert len(calls) == 1
    payload = calls[0]["payload"]
    assert payload.last_verdict == expected_signal
    assert payload.task_id == task_id
    assert payload.repo == "example/repo"


@pytest.mark.asyncio
async def test_give_up_non_fail_decision_does_not_escalate() -> None:
    task_id = uuid.uuid4()
    session = _make_session(step_row=_make_row("wf-ci-fix", task_id), task=_fake_task(task_id))
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await maybe_escalate_operator_on_terminal_give_up(
        session, dispatcher, step_id=str(uuid.uuid4()), typed=_make_typed("pass"),
    )

    assert _escalation_calls(dispatcher) == []


@pytest.mark.asyncio
async def test_give_up_non_terminal_workflow_does_not_escalate() -> None:
    """A wf-review decision=fail is not a give-up workflow — no escalation."""
    task_id = uuid.uuid4()
    session = _make_session(step_row=_make_row("wf-review", task_id), task=_fake_task(task_id))
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await maybe_escalate_operator_on_terminal_give_up(
        session, dispatcher, step_id=str(uuid.uuid4()), typed=_make_typed("fail"),
    )

    assert _escalation_calls(dispatcher) == []


# ── generic emitter ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_no_dispatcher_is_noop() -> None:
    session = AsyncMock()
    await _emit_operator_escalation(
        session, None, task_id=uuid.uuid4(), signal="wf-ci-fix-cap-reached",
    )
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_emit_missing_task_is_noop() -> None:
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await _emit_operator_escalation(
        session, dispatcher, task_id=uuid.uuid4(), signal="wf-ci-fix-cap-reached",
    )

    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_emit_dedup_guard_skips_existing_signal() -> None:
    task_id = uuid.uuid4()
    session = AsyncMock()
    session.get = AsyncMock(return_value=_fake_task(task_id))
    r_dedup = MagicMock()
    r_dedup.first.return_value = MagicMock()  # an existing escalation row
    session.execute = AsyncMock(return_value=r_dedup)
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await _emit_operator_escalation(
        session, dispatcher, task_id=task_id, signal="wf-ci-fix-cap-reached",
    )

    dispatcher.persist_and_publish.assert_not_awaited()


# ── SDE-5: arch cap from wf-author paths now surfaces to operator ─────────────


@pytest.mark.parametrize(
    "fn_name,step_error",
    [
        ("maybe_dispatch_architect_on_author_no_diff",
         "Claude Code produced no changes to commit"),
        ("maybe_dispatch_architect_on_author_remote_rejection",
         "failed to push some refs"),
    ],
)
@pytest.mark.asyncio
async def test_author_cap_surfaces_to_operator(fn_name: str, step_error: str) -> None:
    """SDE-5: when wf-architecture-resolve is capped at a wf-author cap site,
    the cap now calls _emit_arch_cap_reached (previously a silent return None)."""
    from unittest.mock import patch

    import treadmill_api.coordination.triggers as trg

    task_id = uuid.uuid4()
    row = MagicMock()
    row.workflow_id = "wf-author"
    row.run_id = uuid.uuid4()
    row.task_id = task_id
    row.repo = "example/repo"
    row.step_error = step_error

    session = AsyncMock()
    r_step = MagicMock()
    r_step.first.return_value = row
    r_cap = MagicMock()
    r_cap.scalar_one.return_value = trg.ARCHITECTURE_RESOLVE_MAX_ATTEMPTS
    session.execute = AsyncMock(side_effect=[r_step, r_cap])
    dispatcher = MagicMock()

    fn = getattr(trg, fn_name)
    with patch.object(trg, "_emit_arch_cap_reached", new=AsyncMock()) as emit:
        result = await fn(
            session, dispatcher, step_id=str(uuid.uuid4()), workflow_id="wf-author",
        )

    assert result is None
    emit.assert_awaited_once()
    assert emit.await_args.kwargs["task_id"] == task_id
    assert emit.await_args.kwargs["repo"] == "example/repo"


@pytest.mark.asyncio
async def test_emit_defaults_repo_from_task() -> None:
    task_id = uuid.uuid4()
    session = AsyncMock()
    session.get = AsyncMock(return_value=_fake_task(task_id, repo="org/derived"))
    r_dedup = MagicMock()
    r_dedup.first.return_value = None
    session.execute = AsyncMock(return_value=r_dedup)
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await _emit_operator_escalation(
        session, dispatcher, task_id=task_id, signal="wf-doc-amend-cap-reached",
        detail="docs gate stuck",
    )

    calls = _escalation_calls(dispatcher)
    assert len(calls) == 1
    assert calls[0]["payload"].repo == "org/derived"
    assert calls[0]["payload"].last_reasoning == "docs gate stuck"
