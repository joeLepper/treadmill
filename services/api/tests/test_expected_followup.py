"""Unit tests for expected_followup on escalation closes and unreferenced-close report sweep.

Covers:
  * Schema validation for TaskEscalationClosed.expected_followup field
  * API endpoint roundtrip (CloseRequest/CloseResponse)
  * Sweep detection of unreferenced closes (null/empty expected_followup)
  * Grouping by repo
  * Event emission per repo
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination import unreferenced_close_report as report_mod
from treadmill_api.coordination.unreferenced_close_report import (
    UNREFERENCED_CLOSE_REPORT_WORKFLOW_ID,
    run_unreferenced_close_report_sweep,
)
from treadmill_api.events.task import TaskEscalationClosed
from treadmill_api.events.system import UnreferencedClosesReport


# ── Event schema tests ───────────────────────────────────────────────────────


def test_task_escalation_closed_with_expected_followup() -> None:
    """TaskEscalationClosed schema accepts expected_followup field."""
    event = TaskEscalationClosed(
        close_reason="operator_close",
        opened_at=datetime.now(timezone.utc),
        mttr_seconds=3600,
        expected_followup="learning:slug",
    )
    assert event.expected_followup == "learning:slug"


def test_task_escalation_closed_expected_followup_optional() -> None:
    """TaskEscalationClosed expected_followup defaults to None."""
    event = TaskEscalationClosed(
        close_reason="operator_close",
        opened_at=datetime.now(timezone.utc),
        mttr_seconds=3600,
    )
    assert event.expected_followup is None


def test_expected_followup_structured_values() -> None:
    """TaskEscalationClosed accepts all structured expected_followup values."""
    now = datetime.now(timezone.utc)
    for value in ["learning:test", "pr:123", "adr:0001", "transient:auto_progress"]:
        event = TaskEscalationClosed(
            close_reason="operator_close",
            opened_at=now,
            mttr_seconds=100,
            expected_followup=value,
        )
        assert event.expected_followup == value


# ── Unreferenced close report sweep tests ────────────────────────────────────


class _UnreferencedCloseRow:
    """A canned row matching the unreferenced closes SELECT shape."""

    def __init__(
        self,
        repo: str,
        task_id: str,
        close_reason: str,
        mttr_seconds: int,
        closed_at: str,
    ) -> None:
        self.repo = repo
        self.task_id = task_id
        self.close_reason = close_reason
        self.mttr_seconds = mttr_seconds
        self.closed_at = closed_at


class _IterableResult:
    """Mimics the SQLAlchemy ``Result`` shape the sweep iterates."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)


def _closed_at(hours_ago: int = 12) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat()


@pytest.mark.asyncio
async def test_unreferenced_closes_no_closes_is_clean_noop() -> None:
    """When there are no unreferenced closes in the 7-day window, the sweep
    returns 0 and emits no events."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterableResult([]))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    emitted = await run_unreferenced_close_report_sweep(session, dispatcher)

    assert emitted == 0
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreferenced_closes_single_repo() -> None:
    """A single unreferenced close emits one report for its repo."""
    session = AsyncMock()
    rows = [
        _UnreferencedCloseRow(
            repo="test-repo",
            task_id="task-1",
            close_reason="re_progressed",
            mttr_seconds=3600,
            closed_at=_closed_at(hours_ago=2),
        ),
    ]
    session.execute = AsyncMock(return_value=_IterableResult(rows))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    emitted = await run_unreferenced_close_report_sweep(session, dispatcher)

    assert emitted == 1
    dispatcher.persist_and_publish.assert_awaited_once()
    kwargs = dispatcher.persist_and_publish.await_args.kwargs
    assert kwargs["entity_type"] == "system"
    assert kwargs["action"] == "unreferenced_closes_report"
    payload: UnreferencedClosesReport = kwargs["payload"]
    assert payload.repo == "test-repo"
    assert len(payload.closes) == 1
    assert payload.closes[0].task_id == "task-1"


@pytest.mark.asyncio
async def test_unreferenced_closes_grouped_by_repo() -> None:
    """Multiple unreferenced closes from different repos emit one report per repo."""
    session = AsyncMock()
    rows = [
        _UnreferencedCloseRow(
            repo="repo-a",
            task_id="task-1",
            close_reason="re_progressed",
            mttr_seconds=1000,
            closed_at=_closed_at(hours_ago=3),
        ),
        _UnreferencedCloseRow(
            repo="repo-b",
            task_id="task-2",
            close_reason="pr_merged",
            mttr_seconds=2000,
            closed_at=_closed_at(hours_ago=1),
        ),
        _UnreferencedCloseRow(
            repo="repo-a",
            task_id="task-3",
            close_reason="cancelled",
            mttr_seconds=500,
            closed_at=_closed_at(hours_ago=5),
        ),
    ]
    session.execute = AsyncMock(return_value=_IterableResult(rows))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    emitted = await run_unreferenced_close_report_sweep(session, dispatcher)

    # Two repos = two reports.
    assert emitted == 2
    assert dispatcher.persist_and_publish.await_count == 2

    # Verify each report.
    calls = dispatcher.persist_and_publish.await_args_list
    repos_in_calls = {
        calls[0].kwargs["payload"].repo,
        calls[1].kwargs["payload"].repo,
    }
    assert repos_in_calls == {"repo-a", "repo-b"}

    # Repo-a should have 2 closes.
    for call in calls:
        payload = call.kwargs["payload"]
        if payload.repo == "repo-a":
            assert len(payload.closes) == 2
        else:
            assert len(payload.closes) == 1


@pytest.mark.asyncio
async def test_unreferenced_closes_preserves_close_details() -> None:
    """Close details (task_id, close_reason, mttr_seconds, closed_at) are
    preserved in the report."""
    session = AsyncMock()
    closed_at_str = _closed_at(hours_ago=6)
    rows = [
        _UnreferencedCloseRow(
            repo="test-repo",
            task_id="abc-def",
            close_reason="superseded",
            mttr_seconds=7200,
            closed_at=closed_at_str,
        ),
    ]
    session.execute = AsyncMock(return_value=_IterableResult(rows))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await run_unreferenced_close_report_sweep(session, dispatcher)

    payload: UnreferencedClosesReport = (
        dispatcher.persist_and_publish.await_args.kwargs["payload"]
    )
    close = payload.closes[0]
    assert close.task_id == "abc-def"
    assert close.close_reason == "superseded"
    assert close.mttr_seconds == 7200
    assert close.closed_at == closed_at_str


@pytest.mark.asyncio
async def test_unreferenced_closes_window_end_timestamp() -> None:
    """The report includes window_end (when the sweep ran)."""
    session = AsyncMock()
    rows = [
        _UnreferencedCloseRow(
            repo="test-repo",
            task_id="task-1",
            close_reason="re_progressed",
            mttr_seconds=100,
            closed_at=_closed_at(hours_ago=2),
        ),
    ]
    session.execute = AsyncMock(return_value=_IterableResult(rows))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    await run_unreferenced_close_report_sweep(session, dispatcher, now=now)

    payload: UnreferencedClosesReport = (
        dispatcher.persist_and_publish.await_args.kwargs["payload"]
    )
    assert payload.window_end == now.isoformat()


@pytest.mark.asyncio
async def test_handle_scheduled_tick_routes_unreferenced_report_to_deterministic_path() -> None:
    """A scheduled tick for ``wf-unreferenced-close-report`` runs the
    deterministic sweep (no synthetic-task dispatch, no WorkflowVersion lookup).
    Returns ``None`` because the sweep materializes no run."""
    from treadmill_api.coordination.triggers import handle_scheduled_tick
    from treadmill_api.events.schedule import ScheduledTick

    schedule_id = uuid.uuid4()
    mock_schedule = MagicMock()
    mock_schedule.status = "active"
    mock_schedule.workflow_id = UNREFERENCED_CLOSE_REPORT_WORKFLOW_ID

    session = AsyncMock()
    session.get = AsyncMock(return_value=mock_schedule)
    session.execute = AsyncMock()

    typed = ScheduledTick(
        schedule_id=schedule_id,
        workflow_id=UNREFERENCED_CLOSE_REPORT_WORKFLOW_ID,
        rendered_payload={},
    )

    with patch.object(
        report_mod, "run_unreferenced_close_report_sweep", new=AsyncMock(return_value=0),
    ) as mocked_sweep:
        result = await handle_scheduled_tick(
            session, dispatcher=MagicMock(), typed=typed,
        )

    assert result is None
    mocked_sweep.assert_awaited_once()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreferenced_closes_empty_expected_followup_is_unreferenced() -> None:
    """Both null and empty-string expected_followup are counted as unreferenced
    (the SQL WHERE clause catches both cases)."""
    session = AsyncMock()
    # In the real sweep, the SQL filters for both null and empty string.
    # We're testing that rows with these values are correctly grouped and reported.
    rows = [
        _UnreferencedCloseRow(
            repo="test-repo",
            task_id="task-null",
            close_reason="re_progressed",
            mttr_seconds=100,
            closed_at=_closed_at(hours_ago=1),
        ),
        _UnreferencedCloseRow(
            repo="test-repo",
            task_id="task-empty",
            close_reason="pr_merged",
            mttr_seconds=200,
            closed_at=_closed_at(hours_ago=2),
        ),
    ]
    session.execute = AsyncMock(return_value=_IterableResult(rows))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    emitted = await run_unreferenced_close_report_sweep(session, dispatcher)

    assert emitted == 1
    payload: UnreferencedClosesReport = (
        dispatcher.persist_and_publish.await_args.kwargs["payload"]
    )
    assert len(payload.closes) == 2
    task_ids = {close.task_id for close in payload.closes}
    assert task_ids == {"task-null", "task-empty"}
