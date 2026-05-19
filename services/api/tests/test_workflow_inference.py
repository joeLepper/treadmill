"""Unit tests for infer_retry_workflow (ADR-0046 workflow-inference helper).

Tests all four cases specified in the plan:
- Task with only a failed wf-author run → returns 'wf-author'.
- Task with wf-author + wf-feedback both failed → returns 'wf-feedback' (most recent).
- Task with all runs at pr_merged → returns None.
- Task with no runs → returns None.

Mock session.execute returns pre-canned rows; no live Postgres required.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.triggers import infer_retry_workflow


# ── Fixture helpers ────────────────────────────────────────────────────────────


def _run_row(workflow_id: str) -> MagicMock:
    row = MagicMock()
    row.workflow_id = workflow_id
    return row


def _status_row(derived_status: str) -> MagicMock:
    row = MagicMock()
    row.derived_status = derived_status
    return row


def _make_session(run_row: MagicMock | None, status_row: MagicMock | None = None) -> AsyncMock:
    """Build a mock session whose first execute returns run_row (most-recent run
    query) and second execute returns status_row (task_status VIEW query).

    When run_row is None the function should short-circuit and never issue the
    second query; callers that test this path omit status_row.
    """
    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = run_row
    if status_row is not None:
        r2 = MagicMock()
        r2.first.return_value = status_row
        session.execute = AsyncMock(side_effect=[r1, r2])
    else:
        session.execute = AsyncMock(return_value=r1)
    return session


# ── Core test cases ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_wf_author_for_single_failed_run() -> None:
    """Task with only a failed wf-author run → returns 'wf-author'."""
    session = _make_session(
        run_row=_run_row("wf-author"),
        status_row=_status_row("wf-author: failed"),
    )

    result = await infer_retry_workflow(session, uuid.uuid4())

    assert result == "wf-author"


@pytest.mark.asyncio
async def test_returns_most_recent_when_both_author_and_feedback_failed() -> None:
    """Task with wf-author + wf-feedback both failed → returns 'wf-feedback' (most recent)."""
    session = _make_session(
        run_row=_run_row("wf-feedback"),  # most-recent run is wf-feedback
        status_row=_status_row("wf-feedback: failed"),
    )

    result = await infer_retry_workflow(session, uuid.uuid4())

    assert result == "wf-feedback"


@pytest.mark.asyncio
async def test_returns_none_when_task_is_pr_merged() -> None:
    """Task with all runs at pr_merged → returns None."""
    session = _make_session(
        run_row=_run_row("wf-author"),
        status_row=_status_row("pr_merged"),
    )

    result = await infer_retry_workflow(session, uuid.uuid4())

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_no_runs() -> None:
    """Task with no runs → returns None (single query, no status lookup)."""
    session = _make_session(run_row=None)

    result = await infer_retry_workflow(session, uuid.uuid4())

    assert result is None
    assert session.execute.await_count == 1, "must not query task_status when no runs exist"


# ── Additional terminal-status coverage ───────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_none_when_task_cancelled() -> None:
    """Cancelled task → returns None."""
    session = _make_session(
        run_row=_run_row("wf-author"),
        status_row=_status_row("cancelled"),
    )

    result = await infer_retry_workflow(session, uuid.uuid4())

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_task_done() -> None:
    """Task done (no PR) → returns None."""
    session = _make_session(
        run_row=_run_row("wf-validate"),
        status_row=_status_row("done"),
    )

    result = await infer_retry_workflow(session, uuid.uuid4())

    assert result is None


@pytest.mark.asyncio
async def test_returns_workflow_when_task_pr_opened() -> None:
    """Task with PR open and a failed run → returns the workflow_id."""
    session = _make_session(
        run_row=_run_row("wf-validate"),
        status_row=_status_row("pr_opened (wf-validate: failed)"),
    )

    result = await infer_retry_workflow(session, uuid.uuid4())

    assert result == "wf-validate"


@pytest.mark.asyncio
async def test_returns_none_when_task_status_row_missing() -> None:
    """No task_status row (orphaned run) → returns None."""
    session = _make_session(
        run_row=_run_row("wf-author"),
        status_row=None,  # task_status VIEW returns no row
    )
    # Override: first execute returns the run row; second returns None.
    r1 = MagicMock()
    r1.first.return_value = _run_row("wf-author")
    r2 = MagicMock()
    r2.first.return_value = None
    session.execute = AsyncMock(side_effect=[r1, r2])

    result = await infer_retry_workflow(session, uuid.uuid4())

    assert result is None
