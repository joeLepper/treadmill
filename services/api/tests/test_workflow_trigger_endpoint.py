"""Unit tests for the operator workflow-trigger endpoint (ADR-0053 Wave 3).

Tests drive ``trigger_workflow`` against a stub session that holds a
fake workflow-version row, and a mocked ``_create_and_publish_run_without_task``
so we exercise the router's lookup + validation logic without standing up
Postgres + the full dispatch path. The taskless-dispatch internals are
already covered by ``test_workflow_run_taskless.py`` and the
scheduled-tick integration tests.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from treadmill_api.routers import workflow_triggers as wt_router


# ── Stub session ──────────────────────────────────────────────────────────────


class _ScalarOneOrNoneResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSession:
    """Returns ``existing_version`` from ``execute`` (the WorkflowVersion
    lookup), records ``commit`` calls."""

    def __init__(self, *, existing_version: Any | None) -> None:
        self._version = existing_version
        self.committed = False

    async def execute(self, *args: Any, **kwargs: Any) -> _ScalarOneOrNoneResult:
        return _ScalarOneOrNoneResult(self._version)

    async def commit(self) -> None:
        self.committed = True


# ── 404: unknown workflow ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_unknown_workflow_returns_404() -> None:
    session = _StubSession(existing_version=None)
    body = wt_router.WorkflowTriggerRequest(payload={"repo": "acme/example"})
    with pytest.raises(HTTPException) as exc_info:
        await wt_router.trigger_workflow(
            workflow_slug="wf-does-not-exist",
            body=body,
            session=session,  # type: ignore[arg-type]
            dispatcher=MagicMock(),
        )
    assert exc_info.value.status_code == 404
    assert "wf-does-not-exist" in exc_info.value.detail
    assert "not found" in exc_info.value.detail


# ── 400: payload missing repo ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_payload_missing_repo_returns_400() -> None:
    session = _StubSession(existing_version=MagicMock())
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
    """``repo`` present but empty string is rejected the same as missing —
    the taskless dispatch needs a non-empty repo to render step.ready."""
    session = _StubSession(existing_version=MagicMock())
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


# ── 201: happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_happy_path_returns_run_id_and_workflow_id() -> None:
    """Valid payload → 201 with the new run id; shared taskless-dispatch
    helper is invoked with the looked-up workflow_id, the operator trigger
    string, and the repo extracted from the payload."""
    session = _StubSession(existing_version=MagicMock())
    body = wt_router.WorkflowTriggerRequest(
        payload={"repo": "acme/example", "judge_role": "role-validator"},
    )
    expected_run = uuid.uuid4()
    with patch.object(
        wt_router,
        "_create_and_publish_run_without_task",
        new=AsyncMock(return_value=expected_run),
    ) as mocked_dispatch:
        result = await wt_router.trigger_workflow(
            workflow_slug="wf-tune-judge-prompts",
            body=body,
            session=session,  # type: ignore[arg-type]
            dispatcher=MagicMock(),
        )

    assert result.run_id == expected_run
    assert result.workflow_id == "wf-tune-judge-prompts"
    assert session.committed

    mocked_dispatch.assert_awaited_once()
    call_kwargs = mocked_dispatch.await_args.kwargs
    assert call_kwargs["workflow_id"] == "wf-tune-judge-prompts"
    assert call_kwargs["trigger"] == "operator:trigger"
    assert call_kwargs["repo"] == "acme/example"


@pytest.mark.asyncio
async def test_trigger_400_when_dispatch_returns_none() -> None:
    """``_create_and_publish_run_without_task`` returns ``None`` when the
    workflow has no version or no steps — the router translates that to a
    400 instead of silently succeeding."""
    session = _StubSession(existing_version=MagicMock())
    body = wt_router.WorkflowTriggerRequest(payload={"repo": "acme/example"})
    with patch.object(
        wt_router,
        "_create_and_publish_run_without_task",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await wt_router.trigger_workflow(
                workflow_slug="wf-no-steps",
                body=body,
                session=session,  # type: ignore[arg-type]
                dispatcher=MagicMock(),
            )
    assert exc_info.value.status_code == 400
    assert "wf-no-steps" in exc_info.value.detail
