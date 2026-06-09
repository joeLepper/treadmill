"""Unit tests for ``EventProjector`` — the extracted single-writer.

These tests drive the projector against a stub session and verify it
issues the expected SQL statements + returns the expected projection
result. The behavior under test is the same as the test_consumer_unit
projection assertions — this is the lifted-out version per ADR-0011.

See ``services/api/treadmill_api/coordination/event_projector.py`` +
ADR-0084 §"Phase 3C consumer split".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from treadmill_api.coordination.event_projector import (
    EventProjector,
    TaskPRWritten,
)
from treadmill_api.events.step import (
    StepCancelled,
    StepCompleted,
    StepFailed,
    StepSkipped,
    StepStarted,
    StepTokenUsage,
)
from treadmill_api.events.step_output import Artifact, StepOutput


class _StubSession:
    """Records execute calls so we can assert which SQL was emitted."""

    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.scalar = AsyncMock(return_value=None)


def _record(
    *,
    entity_type: str = "step",
    action: str = "started",
    event_id: str | None = None,
    step_id: str | None = None,
    plan_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "action": action,
        "event_id": event_id or str(uuid.uuid4()),
        "step_id": step_id or str(uuid.uuid4()),
        "plan_id": plan_id,
        "task_id": task_id,
        "run_id": run_id,
        "payload": {},
    }


# ── persist_audit_row ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_audit_row_inserts_with_event_id() -> None:
    """A well-formed event_id triggers an INSERT into events."""
    session = _StubSession()
    projector = EventProjector()
    record = _record(event_id=str(uuid.uuid4()))

    await projector.persist_audit_row(session, record, {})

    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_persist_audit_row_skips_missing_event_id() -> None:
    """No event_id → no audit-row INSERT (older publishers)."""
    session = _StubSession()
    projector = EventProjector()
    record = _record()
    record.pop("event_id")  # remove event_id

    await projector.persist_audit_row(session, record, {})

    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_persist_audit_row_skips_malformed_event_id() -> None:
    """A garbage event_id is logged + dropped without an INSERT."""
    session = _StubSession()
    projector = EventProjector()
    record = _record(event_id="not-a-uuid")

    await projector.persist_audit_row(session, record, {})

    assert session.execute.await_count == 0


# ── apply_step_status ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_step_status_started() -> None:
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepStarted(started_at=datetime.now(timezone.utc))

    result = await projector.apply_step_status(
        session, "started", step_id, typed, {},
    )

    assert result is True
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_apply_step_status_completed_with_usage() -> None:
    """token_usage fields populate the five usage columns."""
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(summary="ok", decision="ok", payload={}, artifacts=[]),
        token_usage=StepTokenUsage(
            input_tokens=10,
            output_tokens=20,
            cache_creation_tokens=5,
            cache_read_tokens=2,
            model="claude-opus-4-7",
        ),
    )

    result = await projector.apply_step_status(
        session, "completed", step_id, typed, {},
    )

    assert result is True
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_apply_step_status_completed_without_usage() -> None:
    """Missing token_usage leaves usage columns NULL."""
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(summary="ok", decision="ok", payload={}, artifacts=[]),
        token_usage=None,
    )

    result = await projector.apply_step_status(
        session, "completed", step_id, typed, {},
    )

    assert result is True


@pytest.mark.asyncio
async def test_apply_step_status_failed() -> None:
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepFailed(
        failed_at=datetime.now(timezone.utc),
        error="something broke",
    )

    result = await projector.apply_step_status(
        session, "failed", step_id, typed, {},
    )

    assert result is True
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_apply_step_status_cancelled() -> None:
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepCancelled()

    result = await projector.apply_step_status(
        session, "cancelled", step_id, typed, {},
    )

    assert result is True


@pytest.mark.asyncio
async def test_apply_step_status_skipped() -> None:
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepSkipped(reason="task_terminal", terminal_status="cancelled")

    result = await projector.apply_step_status(
        session, "skipped", step_id, typed, {},
    )

    assert result is True


@pytest.mark.asyncio
async def test_apply_step_status_unknown_action_returns_false() -> None:
    """Unrecognized actions are logged + return False, no SQL."""
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())

    result = await projector.apply_step_status(
        session, "weird-action", step_id, object(), {},
    )

    assert result is False
    assert session.execute.await_count == 0


# ── write_task_prs ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_task_prs_returns_none_for_non_completed() -> None:
    """StepFailed / StepStarted never write task_prs."""
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepFailed(
        failed_at=datetime.now(timezone.utc),
        error="x",
    )

    result = await projector.write_task_prs(session, step_id, typed, {})

    assert result is None


@pytest.mark.asyncio
async def test_write_task_prs_returns_none_when_no_pr_number() -> None:
    """A completed step with no pr_number in payload is a no-op."""
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(summary="ok", decision="ok", payload={}, artifacts=[]),
    )

    result = await projector.write_task_prs(session, step_id, typed, {})

    assert result is None
    # No SQL when payload has no pr_number — short-circuit before the
    # task/repo lookup.
    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_write_task_prs_returns_none_when_no_branch_artifact() -> None:
    """A completed step with pr_number but no branch artifact is a no-op
    with a warning."""
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(
            summary="ok",
            decision="ok",
            payload={"pr_number": 42},
            artifacts=[Artifact(kind="commit_sha", value="abc123")],
        ),
    )

    result = await projector.write_task_prs(session, step_id, typed, {})

    assert result is None
    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_write_task_prs_returns_none_when_no_task_found() -> None:
    """Step not joined to a task → no-op (defensive)."""
    session = _StubSession()
    # session.execute returns a result whose .first() is None
    result_obj = AsyncMock()
    result_obj.first = lambda: None
    session.execute.return_value = result_obj

    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(
            summary="ok",
            decision="ok",
            payload={"pr_number": 42},
            artifacts=[Artifact(kind="branch", value="feat/x")],
        ),
    )

    result = await projector.write_task_prs(session, step_id, typed, {})

    assert result is None


@pytest.mark.asyncio
async def test_write_task_prs_returns_metadata_on_insert() -> None:
    """Happy path: returns TaskPRWritten(task_id, repo, pr_number)."""

    task_id = uuid.uuid4()
    repo = "MediCoderHQ/medicoder"

    class _Row:
        def __init__(self) -> None:
            self.task_id = task_id
            self.repo = repo

    class _FirstResult:
        def first(self) -> _Row:
            return _Row()

    session = _StubSession()
    session.execute.return_value = _FirstResult()

    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(
            summary="ok",
            decision="ok",
            payload={"pr_number": 42},
            artifacts=[Artifact(kind="branch", value="feat/x")],
        ),
    )

    result = await projector.write_task_prs(session, step_id, typed, {})

    assert isinstance(result, TaskPRWritten)
    assert result.task_id == task_id
    assert result.repo == repo
    assert result.pr_number == 42
    # Two execute calls — the SELECT for task/repo + the INSERT.
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_write_task_prs_rejects_boolean_pr_number() -> None:
    """``isinstance(True, int)`` is True in Python — explicit bool check
    prevents a worker bug from writing pr_number=True/False."""
    session = _StubSession()
    projector = EventProjector()
    step_id = str(uuid.uuid4())
    typed = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(
            summary="ok",
            decision="ok",
            payload={"pr_number": True},  # type: ignore[dict-item]
            artifacts=[Artifact(kind="branch", value="feat/x")],
        ),
    )

    result = await projector.write_task_prs(session, step_id, typed, {})

    assert result is None
    assert session.execute.await_count == 0
