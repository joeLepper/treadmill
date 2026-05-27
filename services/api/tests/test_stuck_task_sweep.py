"""Unit tests for the deterministic stuck-task sweep (ADR-0035 P2, ADR-0047).

Three behaviors:

  * A stalled task (latest step event is ``step.completed``, last activity
    older than the threshold, no terminal markers) is detected and one
    ``task.escalated_to_operator`` event is emitted via
    ``_emit_operator_escalation``.
  * A healthy task (recent activity, or a pending downstream step that
    keeps the latest step event off ``completed``) is not flagged. The SQL
    excludes it, so the sweep does not even try to escalate.
  * Idempotency — a task already carrying a ``task.escalated_to_operator``
    event is excluded by the SQL ``NOT EXISTS`` clause, so a second sweep
    does not pile on a second escalation.

We also check the routing seam: ``handle_scheduled_tick`` short-circuits
``wf-stuck-task-sweep`` to ``run_stuck_task_sweep`` instead of looking up a
``WorkflowVersion``.

Pure unit tests with mocked session/dispatcher — no DB, no live LLM.
The integration smoke (live ``*/10`` tick + a real escalation event)
runs out-of-band before merge per the plan.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination import stuck_task_sweep as sweep_mod
from treadmill_api.coordination.stuck_task_sweep import (
    STUCK_TASK_SIGNAL,
    STUCK_TASK_SWEEP_WORKFLOW_ID,
    STUCK_TASK_THRESHOLD,
    run_stuck_task_sweep,
)


class _Row:
    """A canned row matching the sweep's ``SELECT`` shape."""

    def __init__(self, task_id: uuid.UUID, repo: str, last_activity: datetime) -> None:
        self.task_id = task_id
        self.repo = repo
        self.last_step_at = last_activity
        self.last_activity = last_activity


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
    return t


# ── stalled task path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stalled_task_emits_one_escalation() -> None:
    """SQL returns one stalled row → one escalation event with the
    canonical signal + a detail describing the silence interval."""
    task_id = uuid.uuid4()
    stalled_at = datetime.now(timezone.utc) - STUCK_TASK_THRESHOLD - timedelta(minutes=5)

    session = AsyncMock()
    # First execute: the sweep's stalled-tasks SELECT.
    # Second execute: the emitter's per-signal dedup check (no existing).
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_Row(task_id, "example/repo", stalled_at)]),
        _dedup_probe(existing=False),
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_stuck_task_sweep(session, dispatcher)

    assert escalated == 1
    dispatcher.persist_and_publish.assert_awaited_once()
    kwargs = dispatcher.persist_and_publish.await_args.kwargs
    assert kwargs["entity_type"] == "task"
    assert kwargs["action"] == "escalated_to_operator"
    payload = kwargs["payload"]
    assert payload.task_id == task_id
    assert payload.last_verdict == STUCK_TASK_SIGNAL
    assert payload.repo == "example/repo"
    assert payload.last_reasoning is not None
    assert "no downstream step dispatched" in payload.last_reasoning


# ── healthy task path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthy_task_not_flagged() -> None:
    """SQL returns no rows (recent activity or pending downstream step) →
    no escalation attempted, no DB writes beyond the read."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterableResult([]))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_stuck_task_sweep(session, dispatcher)

    assert escalated == 0
    dispatcher.persist_and_publish.assert_not_awaited()
    # The sweep ran exactly one SELECT — no emitter calls means no second
    # session.execute for the dedup lookup.
    assert session.execute.await_count == 1


# ── idempotency: SQL excludes already-escalated tasks ────────────────────────


@pytest.mark.asyncio
async def test_already_escalated_task_excluded_at_sql_layer() -> None:
    """The SQL's ``NOT EXISTS escalated_to_operator`` clause means an
    already-escalated task does not appear in the row stream. The sweep
    therefore makes zero emitter calls — re-running on the next tick does
    not produce a second escalation."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterableResult([]))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_stuck_task_sweep(session, dispatcher)

    assert escalated == 0
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotency_emitter_dedup_guard_blocks_double_emit() -> None:
    """Belt-and-braces: even if the SQL returned a row that already had
    an escalation (e.g. between the SELECT and the emit, another writer
    raced it in), ``_emit_operator_escalation`` reads the dedup row and
    no-ops without persisting a second event."""
    task_id = uuid.uuid4()
    stalled_at = datetime.now(timezone.utc) - STUCK_TASK_THRESHOLD - timedelta(minutes=5)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_Row(task_id, "example/repo", stalled_at)]),
        _dedup_probe(existing=True),  # an existing escalation row
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await run_stuck_task_sweep(session, dispatcher)

    dispatcher.persist_and_publish.assert_not_awaited()


# ── multi-task ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_stalled_tasks_each_escalated_once() -> None:
    """SQL returns N stalled rows → N escalations emitted in one tick."""
    rows = [
        _Row(
            uuid.uuid4(),
            "example/repo",
            datetime.now(timezone.utc) - STUCK_TASK_THRESHOLD - timedelta(minutes=10),
        )
        for _ in range(3)
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

    escalated = await run_stuck_task_sweep(session, dispatcher)

    assert escalated == 3
    assert dispatcher.persist_and_publish.await_count == 3


# ── handle_scheduled_tick wiring ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_scheduled_tick_routes_stuck_sweep_to_deterministic_path() -> None:
    """A scheduled tick for ``wf-stuck-task-sweep`` runs the deterministic
    sweep — it does NOT call the synthetic-task dispatch helper (ADR-0057)
    and therefore does NOT look up a ``WorkflowVersion``. Returns ``None``
    because the sweep materializes no run."""
    from treadmill_api.coordination.triggers import handle_scheduled_tick
    from treadmill_api.events.schedule import ScheduledTick

    schedule_id = uuid.uuid4()
    mock_schedule = MagicMock()
    mock_schedule.status = "active"
    mock_schedule.workflow_id = STUCK_TASK_SWEEP_WORKFLOW_ID

    session = AsyncMock()
    session.get = AsyncMock(return_value=mock_schedule)
    # If the deterministic intercept fires, session.execute MUST NOT be
    # called for the WorkflowVersion lookup. We assert that below.
    session.execute = AsyncMock()

    typed = ScheduledTick(
        schedule_id=schedule_id,
        workflow_id=STUCK_TASK_SWEEP_WORKFLOW_ID,
        rendered_payload={},
    )

    with patch.object(
        sweep_mod, "run_stuck_task_sweep", new=AsyncMock(return_value=0),
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
async def test_handle_scheduled_tick_non_sweep_workflow_uses_role_path() -> None:
    """A non-stuck-sweep schedule slug skips the deterministic intercept
    and falls through to ``_dispatch_via_synthetic_task`` (ADR-0057) —
    we mock the helper here so we don't need a full DB. The deeper
    invariants of the synthetic-task path are covered by
    ``test_scheduled_tick_synthetic_task.py``."""
    from treadmill_api.coordination import triggers as trg
    from treadmill_api.coordination.triggers import handle_scheduled_tick
    from treadmill_api.events.schedule import ScheduledTick

    schedule_id = uuid.uuid4()
    mock_schedule = MagicMock()
    mock_schedule.status = "active"
    mock_schedule.workflow_id = "wf-documentarian-audit"

    session = AsyncMock()
    session.get = AsyncMock(return_value=mock_schedule)

    typed = ScheduledTick(
        schedule_id=schedule_id,
        workflow_id="wf-documentarian-audit",
        rendered_payload={"repo": "example/repo"},
    )

    expected_run = uuid.uuid4()
    with (
        patch.object(
            sweep_mod, "run_stuck_task_sweep", new=AsyncMock(),
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


# ── threshold honored ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_passes_threshold_cutoff_to_sql() -> None:
    """The sweep computes ``now - STUCK_TASK_THRESHOLD`` and binds it as
    the SQL ``:cutoff`` parameter — a regression guard for accidentally
    inverting the comparison or hard-coding a stale cutoff."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    captured: dict[str, Any] = {}

    async def _capture_execute(stmt: Any, params: Any = None) -> Any:
        captured["params"] = params
        return _IterableResult([])

    session = MagicMock()
    session.execute = _capture_execute

    await run_stuck_task_sweep(session, MagicMock(), now=now)

    assert captured["params"] == {"cutoff": now - STUCK_TASK_THRESHOLD}
