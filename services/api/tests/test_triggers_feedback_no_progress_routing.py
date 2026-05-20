"""Unit tests for the wf-feedback no-progress → wf-architecture-resolve trigger
(2026-05-19 dead-end audit, SDE-1).

Sibling of ``test_triggers_feedback_validation_fail_routing.py``. This trigger
owns the no-progress shapes the validation-fail sibling does NOT:
  * ``decision='responded-without-change'``
  * a bare ``decision='fail'`` with no ``validation_results`` fail entry
…on a task with no open PR (PR-bearing cases stay on the deadlock path).

Pure unit tests with mocked session/dispatcher.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination.triggers import (
    maybe_dispatch_architect_on_feedback_no_progress,
)
from treadmill_api.events.step import StepCompleted
from treadmill_api.events.step_output import Metadata, StepOutput


def _make_typed(
    *, decision: str, validation_results: list[dict[str, Any]] | None = None,
) -> StepCompleted:
    payload: dict[str, Any] = {}
    if validation_results is not None:
        payload["validation_results"] = validation_results
    return StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(
            summary="feedback terminal",
            decision=decision,
            commit_sha=None,
            artifacts=[],
            payload=payload,
            metadata=Metadata(),
        ),
    )


def _make_row(
    *, workflow_id: str = "wf-feedback", step_name: str = "action",
    task_id: uuid.UUID | None = None, repo: str = "example/repo",
) -> MagicMock:
    row = MagicMock()
    row.workflow_id = workflow_id
    row.run_id = uuid.uuid4()
    row.task_id = task_id or uuid.uuid4()
    row.repo = repo
    row.step_name = step_name
    return row


def _fake_task(task_id: uuid.UUID, repo: str = "example/repo") -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.repo = repo
    return t


def _make_session(*, row: Any, open_pr_row: Any = None, cap_count: int = 0, task: Any = None) -> AsyncMock:
    session = AsyncMock()
    r1 = MagicMock(); r1.first.return_value = row
    r2 = MagicMock(); r2.first.return_value = open_pr_row
    r3 = MagicMock(); r3.scalar_one.return_value = cap_count
    session.execute = AsyncMock(side_effect=[r1, r2, r3])
    session.get = AsyncMock(return_value=task)
    return session


async def _run(session: AsyncMock, dispatcher: Any, typed: StepCompleted, step_id: str) -> tuple[Any, list[dict[str, Any]], dict[str, Any]]:
    dispatched: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}

    async def _stub_dedup(session: Any, *, workflow_id: str, payload: Any, dispatch_fn: Any) -> uuid.UUID:
        dispatched.append({"workflow_id": workflow_id, "payload": payload})
        return await dispatch_fn()

    async def _stub_create(session: Any, dispatcher: Any, *, task: Any, workflow_id: str, trigger: str, source_step_id: Any = None) -> uuid.UUID:
        captured["trigger"] = trigger
        captured["source_step_id"] = source_step_id
        return uuid.uuid4()

    with (
        patch("treadmill_api.coordination.triggers.maybe_dispatch_with_dedup", side_effect=_stub_dedup),
        patch("treadmill_api.coordination.triggers._create_and_publish_run", side_effect=_stub_create),
    ):
        result = await maybe_dispatch_architect_on_feedback_no_progress(
            session, dispatcher, step_id=step_id, typed=typed,
        )
    return result, dispatched, captured


@pytest.mark.asyncio
async def test_responded_without_change_no_pr_dispatches() -> None:
    task_id = uuid.uuid4()
    session = _make_session(row=_make_row(task_id=task_id), task=_fake_task(task_id))
    step_id = str(uuid.uuid4())
    result, dispatched, captured = await _run(
        session, MagicMock(), _make_typed(decision="responded-without-change"), step_id,
    )
    assert result is not None
    assert len(dispatched) == 1
    assert dispatched[0]["workflow_id"] == "wf-architecture-resolve"
    assert dispatched[0]["payload"]["feedback_no_progress_step_id"] == step_id
    assert captured["trigger"] == "self:wf-feedback-no-progress"
    assert captured["source_step_id"] == step_id


@pytest.mark.asyncio
async def test_bare_fail_no_validation_results_dispatches() -> None:
    task_id = uuid.uuid4()
    session = _make_session(row=_make_row(task_id=task_id), task=_fake_task(task_id))
    result, dispatched, _ = await _run(
        session, MagicMock(), _make_typed(decision="fail"), str(uuid.uuid4()),
    )
    assert result is not None
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_fail_with_validation_results_is_skipped() -> None:
    """The validation-fail sibling owns decision=fail + validation_results."""
    session = _make_session(row=_make_row(), task=_fake_task(uuid.uuid4()))
    result, dispatched, _ = await _run(
        session, MagicMock(),
        _make_typed(decision="fail", validation_results=[{"verdict": "fail"}]),
        str(uuid.uuid4()),
    )
    assert result is None
    assert dispatched == []


@pytest.mark.asyncio
async def test_open_pr_defers_to_deadlock() -> None:
    task_id = uuid.uuid4()
    open_pr = MagicMock(); open_pr.pr_number = 7
    session = _make_session(row=_make_row(task_id=task_id), open_pr_row=open_pr, task=_fake_task(task_id))
    result, dispatched, _ = await _run(
        session, MagicMock(), _make_typed(decision="responded-without-change"), str(uuid.uuid4()),
    )
    assert result is None
    assert dispatched == []


@pytest.mark.asyncio
async def test_pushed_decision_skipped() -> None:
    session = AsyncMock()  # should never query — cheap decision filter first
    result, dispatched, _ = await _run(
        session, MagicMock(), _make_typed(decision="pushed"), str(uuid.uuid4()),
    )
    assert result is None
    assert dispatched == []


@pytest.mark.asyncio
async def test_non_feedback_workflow_skipped() -> None:
    session = _make_session(row=_make_row(workflow_id="wf-author"), task=_fake_task(uuid.uuid4()))
    result, dispatched, _ = await _run(
        session, MagicMock(), _make_typed(decision="fail"), str(uuid.uuid4()),
    )
    assert result is None
    assert dispatched == []


@pytest.mark.asyncio
async def test_non_action_step_skipped() -> None:
    session = _make_session(row=_make_row(step_name="analyzer"), task=_fake_task(uuid.uuid4()))
    result, dispatched, _ = await _run(
        session, MagicMock(), _make_typed(decision="responded-without-change"), str(uuid.uuid4()),
    )
    assert result is None
    assert dispatched == []
