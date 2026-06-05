"""Unit tests for the deterministic terminal-gate orphan sweep (ADR-0047,
ADR-0038, ADR-0042).

Behaviors:

  * An orphaned PR (task has a ``review.override`` or ``validate.override``
    event, no ``github.pr_merged``, no ``task.cancelled`` / ``task.superseded``,
    no prior escalation for this signal) is detected and one
    ``task.escalated_to_operator`` event is emitted via
    ``_emit_operator_escalation``.
  * A task with a merged PR is not flagged. The SQL excludes it, so the
    sweep does not even try to escalate.
  * Idempotency — a task already carrying a ``task.escalated_to_operator``
    event for the specific ``TERMINAL_GATE_SIGNAL`` is excluded by the SQL
    ``NOT EXISTS`` clause, so a second sweep does not pile on a second
    escalation.
  * Cancelled and superseded tasks are explicitly excluded — they legitimately
    leave a PR unmerged.

We also check the routing seam: ``handle_scheduled_tick`` short-circuits
``wf-terminal-gate-sweep`` to ``run_terminal_gate_sweep`` instead of looking
up a ``WorkflowVersion``.

Pure unit tests with mocked session/dispatcher — no DB, no live LLM.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination import terminal_gate_sweep as sweep_mod
from treadmill_api.coordination.terminal_gate_sweep import (
    TERMINAL_GATE_SIGNAL,
    TERMINAL_GATE_SWEEP_WORKFLOW_ID,
    run_terminal_gate_sweep,
)


class _Row:
    """A canned row matching the sweep's ``SELECT`` shape."""

    def __init__(
        self,
        task_id: uuid.UUID,
        repo: str,
        override_verb: str,
        pr_number: int | None,
    ) -> None:
        self.task_id = task_id
        self.repo = repo
        self.override_verb = override_verb
        self.pr_number = pr_number


class _IterableResult:
    """Mimics the SQLAlchemy ``Result`` shape the sweep iterates."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)


def _dedup_probe(existing: bool) -> MagicMock:
    """The emitter's per-signal dedup ``SELECT`` returns a result whose
    ``.first()`` is either ``None`` (proceed with emit) or a row (skip)."""
    r = MagicMock()
    r.first.return_value = MagicMock() if existing else None
    return r


def _fake_task(task_id: uuid.UUID, repo: str = "example/repo") -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.repo = repo
    t.plan_id = uuid.uuid4()
    t.created_by = "test-operator"  # str|None — escalation payload validates
    return t


# ── orphaned PR path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_override_orphaned_pr_emits_escalation() -> None:
    """SQL returns one row with review.override → one escalation event with
    the canonical signal + a detail naming the PR number and override verb."""
    task_id = uuid.uuid4()

    session = AsyncMock()
    # First execute: the sweep's orphaned-PRs SELECT.
    # Second execute: the emitter's per-signal dedup check (no existing).
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_Row(task_id, "example/repo", "review.override", 42)]),
        _dedup_probe(existing=False),
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_terminal_gate_sweep(session, dispatcher)

    assert escalated == 1
    dispatcher.persist_and_publish.assert_awaited_once()
    kwargs = dispatcher.persist_and_publish.await_args.kwargs
    assert kwargs["entity_type"] == "task"
    assert kwargs["action"] == "escalated_to_operator"
    payload = kwargs["payload"]
    assert payload.task_id == task_id
    assert payload.last_verdict == TERMINAL_GATE_SIGNAL
    assert payload.repo == "example/repo"
    assert payload.reason == "terminal_gate_sweep"
    assert payload.last_reasoning is not None
    assert "PR#42" in payload.last_reasoning
    assert "review.override" in payload.last_reasoning


@pytest.mark.asyncio
async def test_validate_override_orphaned_pr_emits_escalation() -> None:
    """validate.override is also an accept-as-is signal; it emits the same
    escalation with 'validate.override' in the detail string."""
    task_id = uuid.uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_Row(task_id, "example/repo", "validate.override", 99)]),
        _dedup_probe(existing=False),
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_terminal_gate_sweep(session, dispatcher)

    assert escalated == 1
    kwargs = dispatcher.persist_and_publish.await_args.kwargs
    payload = kwargs["payload"]
    assert "PR#99" in payload.last_reasoning
    assert "validate.override" in payload.last_reasoning


@pytest.mark.asyncio
async def test_unknown_pr_number_uses_fallback_text() -> None:
    """When task_prs has no row (LEFT JOIN returns NULL), the detail uses
    'PR (number unknown)' rather than crashing."""
    task_id = uuid.uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_Row(task_id, "example/repo", "review.override", None)]),
        _dedup_probe(existing=False),
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_terminal_gate_sweep(session, dispatcher)

    assert escalated == 1
    payload = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    assert "PR (number unknown)" in payload.last_reasoning


# ── tasks that must NOT be flagged ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_orphaned_prs_short_circuits() -> None:
    """SQL returns no rows (merged / cancelled / superseded / never overridden)
    → no escalation attempted, no DB writes beyond the read."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterableResult([]))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_terminal_gate_sweep(session, dispatcher)

    assert escalated == 0
    dispatcher.persist_and_publish.assert_not_awaited()
    # The sweep ran exactly one SELECT — no emitter calls means no second
    # session.execute for the dedup lookup.
    assert session.execute.await_count == 1


# ── idempotency ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_already_escalated_task_excluded_at_sql_layer() -> None:
    """The SQL's signal-specific ``NOT EXISTS escalated_to_operator`` clause
    means a task already carrying this escalation does not appear in the row
    stream. The sweep therefore makes zero emitter calls."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterableResult([]))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_terminal_gate_sweep(session, dispatcher)

    assert escalated == 0
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotency_emitter_dedup_guard_blocks_double_emit() -> None:
    """Belt-and-braces: even if the SQL returned a row that already had
    an escalation (e.g. between the SELECT and the emit, another writer
    raced it in), ``_emit_operator_escalation`` reads the dedup row and
    no-ops without persisting a second event."""
    task_id = uuid.uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_Row(task_id, "example/repo", "review.override", 7)]),
        _dedup_probe(existing=True),  # an existing escalation row
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await run_terminal_gate_sweep(session, dispatcher)

    dispatcher.persist_and_publish.assert_not_awaited()


# ── multi-PR batch ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_orphaned_prs_each_escalated_once() -> None:
    """SQL returns N orphaned rows → N escalations emitted in one tick."""
    rows = [
        _Row(uuid.uuid4(), "example/repo", "review.override", i + 1)
        for i in range(3)
    ]
    session = AsyncMock()
    # One sweep SELECT + one dedup SELECT per row.
    session.execute = AsyncMock(side_effect=[
        _IterableResult(rows),
        _dedup_probe(existing=False),
        _dedup_probe(existing=False),
        _dedup_probe(existing=False),
    ])
    session.get = AsyncMock(side_effect=[_fake_task(r.task_id) for r in rows])

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_terminal_gate_sweep(session, dispatcher)

    assert escalated == 3
    assert dispatcher.persist_and_publish.await_count == 3


# ── handle_scheduled_tick wiring ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_scheduled_tick_routes_terminal_gate_sweep_to_deterministic_path() -> None:
    """A scheduled tick for ``wf-terminal-gate-sweep`` runs the deterministic
    sweep — it does NOT call the synthetic-task dispatch helper (ADR-0057)
    and therefore does NOT look up a ``WorkflowVersion``. Returns ``None``
    because the sweep materializes no run."""
    from treadmill_api.coordination.triggers import handle_scheduled_tick
    from treadmill_api.events.schedule import ScheduledTick

    schedule_id = uuid.uuid4()
    mock_schedule = MagicMock()
    mock_schedule.status = "active"
    mock_schedule.workflow_id = TERMINAL_GATE_SWEEP_WORKFLOW_ID

    session = AsyncMock()
    session.get = AsyncMock(return_value=mock_schedule)
    # If the deterministic intercept fires, session.execute MUST NOT be
    # called for the WorkflowVersion lookup. We assert that below.
    session.execute = AsyncMock()

    typed = ScheduledTick(
        schedule_id=schedule_id,
        workflow_id=TERMINAL_GATE_SWEEP_WORKFLOW_ID,
        rendered_payload={},
    )

    with patch.object(
        sweep_mod, "run_terminal_gate_sweep", new=AsyncMock(return_value=0),
    ) as mocked_sweep:
        result = await handle_scheduled_tick(
            session, dispatcher=MagicMock(), typed=typed,
        )

    assert result is None
    mocked_sweep.assert_awaited_once()
    # The deterministic intercept must short-circuit before the
    # _create_and_publish_run_without_task path issues its
    # WorkflowVersion SELECT.
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_scheduled_tick_non_terminal_gate_slug_not_intercepted() -> None:
    """A non-terminal-gate schedule slug skips the deterministic intercept
    and falls through to ``_dispatch_via_synthetic_task`` (ADR-0057) —
    we mock the helper here so we don't need a full DB."""
    from treadmill_api.coordination import triggers as trg
    from treadmill_api.coordination.triggers import handle_scheduled_tick
    from treadmill_api.events.schedule import ScheduledTick

    schedule_id = uuid.uuid4()
    mock_schedule = MagicMock()
    mock_schedule.status = "active"
    mock_schedule.workflow_id = "wf-documentarian-audit"

    mock_wv = MagicMock()
    mock_wv.id = uuid.uuid4()
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none = MagicMock(return_value=mock_wv)
    mock_execute_result.scalars = MagicMock(return_value=iter([]))

    session = AsyncMock()
    session.get = AsyncMock(return_value=mock_schedule)
    session.execute = AsyncMock(return_value=mock_execute_result)

    typed = ScheduledTick(
        schedule_id=schedule_id,
        workflow_id="wf-documentarian-audit",
        rendered_payload={"repo": "example/repo"},
    )

    expected_run = uuid.uuid4()
    with (
        patch.object(
            sweep_mod, "run_terminal_gate_sweep", new=AsyncMock(),
        ) as mocked_sweep,
        patch.object(
            trg,
            "_dispatch_via_synthetic_task",
            new=AsyncMock(return_value=expected_run),
        ) as mocked_dispatch,
    ):
        result = await handle_scheduled_tick(
            session, dispatcher=MagicMock(), typed=typed,
        )

    assert result == expected_run
    mocked_sweep.assert_not_awaited()
    mocked_dispatch.assert_awaited_once()


# ── signal constant ───────────────────────────────────────────────────────────


def test_terminal_gate_signal_value() -> None:
    """The signal value is the dedup key stored in ``last_verdict`` and the
    SQL's ``:signal`` bind parameter. Pin it so renaming does not silently
    break the NOT EXISTS dedup clause."""
    assert TERMINAL_GATE_SIGNAL == "wf-terminal-gate-sweep-orphaned-pr"


def test_terminal_gate_sweep_workflow_id_value() -> None:
    """Pin the workflow_id slug so scheduler seed and handle_scheduled_tick
    interception can't drift apart."""
    assert TERMINAL_GATE_SWEEP_WORKFLOW_ID == "wf-terminal-gate-sweep"
