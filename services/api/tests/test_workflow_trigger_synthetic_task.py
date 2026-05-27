"""Unit tests for the operator workflow-trigger endpoint's synthetic-task
path (ADR-0057).

The pre-fix endpoint used ``_create_and_publish_run_without_task`` — the
4th silent-failure pattern in the scheduler primitive. The fix routes
the endpoint through ``_dispatch_via_synthetic_task`` so workers always
see a task-bound envelope.

These tests verify the router's contract on top of a mocked helper:
  * 400 when ``payload.repo`` is missing or empty (router-local check).
  * 400 when the helper returns ``None`` (no workflow version / no steps).
  * 201 on the happy path returns BOTH ``task_id`` and ``run_id`` (new
    field per ADR-0057 — operators need the task id to grep the audit log).

We mock ``_dispatch_via_synthetic_task`` so the helper's own internals
(WorkflowVersion lookup, Task creation, dispatch_task) are covered by
``test_scheduled_tick_synthetic_task.py`` instead. The endpoint test
exercises the router's wiring — payload validation, error contract,
response shape — without standing up the dispatcher.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from treadmill_api.routers import workflow_triggers as wt_router


# ── Stub session ──────────────────────────────────────────────────────────────


class _ScalarOneResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _StubSession:
    """Async session double: ``execute`` returns the seeded WorkflowRun row
    (used by the router to read back the synthetic ``task_id``)."""

    def __init__(self, *, run_row: Any | None = None) -> None:
        self._run_row = run_row
        self.committed = False

    async def execute(self, *args: Any, **kwargs: Any) -> _ScalarOneResult:
        return _ScalarOneResult(self._run_row)

    async def commit(self) -> None:
        self.committed = True


# ── 400: payload missing repo ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_payload_missing_repo_returns_400() -> None:
    session = _StubSession()
    body = wt_router.WorkflowTriggerRequest(payload={"foo": "bar"})
    with pytest.raises(HTTPException) as exc_info:
        await wt_router.trigger_workflow(
            workflow_slug="wf-tune-judge-prompts",
            body=body,
            session=session,  # type: ignore[arg-type]
            dispatcher=MagicMock(),
        )
    assert exc_info.value.status_code == 400
    assert "repo" in exc_info.value.detail


@pytest.mark.asyncio
async def test_trigger_payload_empty_repo_returns_400() -> None:
    session = _StubSession()
    body = wt_router.WorkflowTriggerRequest(payload={"repo": ""})
    with pytest.raises(HTTPException) as exc_info:
        await wt_router.trigger_workflow(
            workflow_slug="wf-tune-judge-prompts",
            body=body,
            session=session,  # type: ignore[arg-type]
            dispatcher=MagicMock(),
        )
    assert exc_info.value.status_code == 400
    assert "repo" in exc_info.value.detail


# ── 400: helper returns None (unknown workflow / no steps) ───────────────────


@pytest.mark.asyncio
async def test_trigger_returns_400_when_workflow_unseeded() -> None:
    """ADR-0057: an un-seeded workflow makes ``_dispatch_via_synthetic_task``
    return None (no WorkflowVersion); the endpoint converts that into a
    400 with a slug-mentioning detail."""
    session = _StubSession()
    body = wt_router.WorkflowTriggerRequest(payload={"repo": "acme/example"})
    with patch.object(
        wt_router,
        "_dispatch_via_synthetic_task",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await wt_router.trigger_workflow(
                workflow_slug="wf-does-not-exist",
                body=body,
                session=session,  # type: ignore[arg-type]
                dispatcher=MagicMock(),
            )
    assert exc_info.value.status_code == 400
    assert "wf-does-not-exist" in exc_info.value.detail


# ── 201: happy path returns task_id AND run_id ───────────────────────────────


@pytest.mark.asyncio
async def test_trigger_happy_path_returns_task_id_and_run_id() -> None:
    """Per ADR-0057, the response carries both ``task_id`` and ``run_id``
    so operators can grep events / audit log for the dispatch they just
    fired. The router reads the task_id off the new WorkflowRun row."""
    expected_run_id = uuid.uuid4()
    expected_task_id = uuid.uuid4()
    run_row = MagicMock()
    run_row.id = expected_run_id
    run_row.task_id = expected_task_id
    session = _StubSession(run_row=run_row)

    body = wt_router.WorkflowTriggerRequest(
        payload={"repo": "acme/example", "judge_role": "role-validator"},
    )

    with patch.object(
        wt_router,
        "_dispatch_via_synthetic_task",
        new=AsyncMock(return_value=expected_run_id),
    ) as mocked_helper:
        resp = await wt_router.trigger_workflow(
            workflow_slug="wf-tune-judge-prompts",
            body=body,
            session=session,  # type: ignore[arg-type]
            dispatcher=MagicMock(),
        )

    assert resp.run_id == expected_run_id
    assert resp.task_id == expected_task_id
    assert resp.workflow_id == "wf-tune-judge-prompts"
    assert session.committed is True

    # Helper invoked with the expected operator-trigger signature.
    mocked_helper.assert_awaited_once()
    call_kwargs = mocked_helper.await_args.kwargs
    assert call_kwargs["workflow_id"] == "wf-tune-judge-prompts"
    assert call_kwargs["repo"] == "acme/example"
    assert call_kwargs["trigger"] == "operator:trigger"
    assert call_kwargs["created_by"] == "operator-trigger"
