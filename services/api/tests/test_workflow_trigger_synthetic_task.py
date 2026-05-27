"""Unit tests for the synthetic-task path in the workflow-trigger endpoint.

Per ADR-0057, ``POST /api/v1/workflows/{slug}/trigger`` now creates a
synthetic ``Task`` with ``created_by="operator-trigger"`` and returns
``{task_id, run_id, workflow_id}`` instead of the old ``{run_id, workflow_id}``.

Tests drive the route handler directly with mocked session + dispatcher,
mirroring the pattern in ``test_routers_unit.py``.

Validation:
  ``cd services/api && uv run pytest tests/test_workflow_trigger_synthetic_task.py -q``
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from treadmill_api.models import Task
from treadmill_api.routers.workflow_triggers import (
    WorkflowTriggerRequest,
    trigger_workflow,
)
from treadmill_api.seed.system_plan import SYSTEM_PLAN_ID


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_wv(workflow_id: str = "wf-tune-judge-prompts") -> MagicMock:
    wv = MagicMock()
    wv.id = uuid.uuid4()
    wv.workflow_id = workflow_id
    return wv


def _make_session(
    wv: object = None,
    system_plan_present: bool = True,
) -> AsyncMock:
    """Async session mock.

    ``session.execute(...)`` returns a result whose ``scalar_one_or_none``
    yields the given ``wv``.  ``session.get(...)`` returns a truthy mock when
    ``system_plan_present`` is ``True`` (simulating the seeded system Plan).
    """
    session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = wv
    session.execute.return_value = mock_result

    async def _get(model, pk):
        if pk == SYSTEM_PLAN_ID:
            return MagicMock() if system_plan_present else None
        return None

    session.get.side_effect = _get

    return session


def _make_dispatcher(run_id: uuid.UUID | None = None) -> AsyncMock:
    d = AsyncMock()
    d.dispatch_task.return_value = run_id or uuid.uuid4()
    return d


# ── happy path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_workflow_response_has_task_id() -> None:
    """Response includes a non-None ``task_id``."""
    wv = _make_wv()
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    response = await trigger_workflow(
        workflow_slug="wf-tune-judge-prompts",
        body=body,
        session=session,
        dispatcher=dispatcher,
    )

    assert response.task_id is not None
    assert isinstance(response.task_id, uuid.UUID)


@pytest.mark.asyncio
async def test_trigger_workflow_response_has_run_id() -> None:
    expected_run_id = uuid.uuid4()
    wv = _make_wv()
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher(run_id=expected_run_id)

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    response = await trigger_workflow(
        workflow_slug="wf-tune-judge-prompts",
        body=body,
        session=session,
        dispatcher=dispatcher,
    )

    assert response.run_id == expected_run_id


@pytest.mark.asyncio
async def test_trigger_workflow_response_has_workflow_id() -> None:
    wv = _make_wv("wf-tune-judge-prompts")
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    response = await trigger_workflow(
        workflow_slug="wf-tune-judge-prompts",
        body=body,
        session=session,
        dispatcher=dispatcher,
    )

    assert response.workflow_id == "wf-tune-judge-prompts"


@pytest.mark.asyncio
async def test_trigger_workflow_task_created_by_operator_trigger() -> None:
    """The Task row has ``created_by="operator-trigger"``."""
    wv = _make_wv()
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    await trigger_workflow(
        workflow_slug="wf-tune-judge-prompts",
        body=body,
        session=session,
        dispatcher=dispatcher,
    )

    added = [c.args[0] for c in session.add.call_args_list]
    tasks = [o for o in added if isinstance(o, Task)]
    assert len(tasks) == 1
    assert tasks[0].created_by == "operator-trigger"


@pytest.mark.asyncio
async def test_trigger_workflow_task_plan_id_is_system_plan() -> None:
    wv = _make_wv()
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    await trigger_workflow(
        workflow_slug="wf-tune-judge-prompts",
        body=body,
        session=session,
        dispatcher=dispatcher,
    )

    added = [c.args[0] for c in session.add.call_args_list]
    task = next(o for o in added if isinstance(o, Task))
    assert task.plan_id == SYSTEM_PLAN_ID


@pytest.mark.asyncio
async def test_trigger_workflow_task_repo_from_payload() -> None:
    wv = _make_wv()
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={"repo": "myorg/myrepo"})
    await trigger_workflow(
        workflow_slug="wf-tune-judge-prompts",
        body=body,
        session=session,
        dispatcher=dispatcher,
    )

    added = [c.args[0] for c in session.add.call_args_list]
    task = next(o for o in added if isinstance(o, Task))
    assert task.repo == "myorg/myrepo"


@pytest.mark.asyncio
async def test_trigger_workflow_calls_dispatch_task() -> None:
    wv = _make_wv()
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    await trigger_workflow(
        workflow_slug="wf-tune-judge-prompts",
        body=body,
        session=session,
        dispatcher=dispatcher,
    )

    dispatcher.dispatch_task.assert_called_once()


@pytest.mark.asyncio
async def test_trigger_workflow_commits_session() -> None:
    wv = _make_wv()
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    await trigger_workflow(
        workflow_slug="wf-tune-judge-prompts",
        body=body,
        session=session,
        dispatcher=dispatcher,
    )

    session.commit.assert_called_once()


# ── error / guard conditions ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_workflow_404_when_no_workflow_version() -> None:
    session = _make_session(wv=None)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    with pytest.raises(HTTPException) as exc_info:
        await trigger_workflow(
            workflow_slug="wf-unknown",
            body=body,
            session=session,
            dispatcher=dispatcher,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_trigger_workflow_400_when_repo_missing_from_payload() -> None:
    wv = _make_wv()
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={})  # no repo
    with pytest.raises(HTTPException) as exc_info:
        await trigger_workflow(
            workflow_slug="wf-tune-judge-prompts",
            body=body,
            session=session,
            dispatcher=dispatcher,
        )

    assert exc_info.value.status_code == 400
    assert "repo" in exc_info.value.detail


@pytest.mark.asyncio
async def test_trigger_workflow_503_when_system_plan_not_seeded() -> None:
    wv = _make_wv()
    session = _make_session(wv=wv, system_plan_present=False)
    dispatcher = _make_dispatcher()

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    with pytest.raises(HTTPException) as exc_info:
        await trigger_workflow(
            workflow_slug="wf-tune-judge-prompts",
            body=body,
            session=session,
            dispatcher=dispatcher,
        )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_trigger_workflow_400_on_dispatch_error() -> None:
    from treadmill_api.dispatch import DispatchError

    wv = _make_wv()
    session = _make_session(wv=wv)
    dispatcher = _make_dispatcher()
    dispatcher.dispatch_task.side_effect = DispatchError("no steps")

    body = WorkflowTriggerRequest(payload={"repo": "testorg/repo"})
    with pytest.raises(HTTPException) as exc_info:
        await trigger_workflow(
            workflow_slug="wf-tune-judge-prompts",
            body=body,
            session=session,
            dispatcher=dispatcher,
        )

    assert exc_info.value.status_code == 400
    assert "no steps" in exc_info.value.detail
