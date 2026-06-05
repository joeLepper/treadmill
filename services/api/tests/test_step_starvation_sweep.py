"""Unit tests for the step-starvation sweep (ADR-0075).

Four behaviors:

  * A stalled step (state is ``step.ready``, no later ``step.started``
    for the same (task, step_index), ready event older than the
    threshold) is detected and one ``task.escalated_to_operator`` event
    is emitted via ``_emit_operator_escalation``.
  * A healthy step (recent ready event, or a started event exists later)
    is not flagged. The SQL excludes it, so the sweep does not even try
    to escalate.
  * Idempotency — a task already carrying a ``task.escalated_to_operator``
    event is excluded by the SQL ``NOT EXISTS`` clause, so a second sweep
    does not pile on a second escalation.
  * Multiple stalled steps: each gets its own escalation event.

We also check the routing seam: ``handle_scheduled_tick`` short-circuits
``wf-step-starvation-sweep`` to ``run_step_starvation_sweep`` instead of
looking up a ``WorkflowVersion``.

Pure unit tests with mocked session/dispatcher — no DB, no live LLM.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination import step_starvation_sweep as sweep_mod
from treadmill_api.coordination.step_starvation_sweep import (
    STEP_STARVATION_SIGNAL,
    STEP_STARVATION_SWEEP_WORKFLOW_ID,
    STEP_STARVATION_THRESHOLD,
    run_step_starvation_sweep,
)


class _Row:
    """A canned row matching the sweep's ``SELECT`` shape."""

    def __init__(
        self,
        task_id: uuid.UUID,
        repo: str,
        step_name: str,
        role_id: str,
        ready_at: datetime,
    ) -> None:
        self.task_id = task_id
        self.repo = repo
        self.step_name = step_name
        self.role_id = role_id
        self.ready_at = ready_at


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


# ── stalled step path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stalled_step_emits_one_escalation() -> None:
    """SQL returns one stalled row → one escalation event with the
    canonical signal + a detail describing the stall duration and step."""
    task_id = uuid.uuid4()
    ready_at = datetime.now(timezone.utc) - STEP_STARVATION_THRESHOLD - timedelta(minutes=1)
    now = datetime.now(timezone.utc)

    session = AsyncMock()
    # First execute: the sweep's stalled-steps SELECT.
    # Second execute: the emitter's per-signal dedup check (no existing).
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_Row(task_id, "example/repo", "analyze", "role-architect", ready_at)]),
        _dedup_probe(existing=False),
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_step_starvation_sweep(session, dispatcher, now=now)

    assert escalated == 1
    dispatcher.persist_and_publish.assert_awaited_once()
    kwargs = dispatcher.persist_and_publish.await_args.kwargs
    assert kwargs["entity_type"] == "task"
    assert kwargs["action"] == "escalated_to_operator"
    payload = kwargs["payload"]
    assert payload.task_id == task_id
    assert payload.last_verdict == STEP_STARVATION_SIGNAL
    assert payload.repo == "example/repo"
    assert payload.last_reasoning is not None
    assert "analyze" in payload.last_reasoning
    assert "role-architect" in payload.last_reasoning
    assert "never started" in payload.last_reasoning


# ── healthy step path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthy_step_not_flagged() -> None:
    """SQL returns no rows (recent ready event or started event exists) →
    no escalation attempted, no DB writes beyond the read."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterableResult([]))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_step_starvation_sweep(session, dispatcher)

    assert escalated == 0
    dispatcher.persist_and_publish.assert_not_awaited()
    # The sweep ran exactly one SELECT — no emitter calls means no second
    # session.execute for the dedup lookup.
    assert session.execute.await_count == 1


# ── ready event not yet stale ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_ready_event_not_flagged() -> None:
    """SQL filters by cutoff (now - threshold); a ready event younger
    than the threshold does not appear in the row stream. The sweep makes
    zero emitter calls."""
    now = datetime.now(timezone.utc)
    recent_ready = now - STEP_STARVATION_THRESHOLD + timedelta(seconds=10)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterableResult([]))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_step_starvation_sweep(session, dispatcher, now=now)

    assert escalated == 0
    dispatcher.persist_and_publish.assert_not_awaited()


# ── idempotency: SQL excludes already-escalated tasks ─────────────────────


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

    escalated = await run_step_starvation_sweep(session, dispatcher)

    assert escalated == 0
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotency_emitter_dedup_guard_blocks_double_emit() -> None:
    """Belt-and-braces: even if the SQL returned a row that already had
    an escalation (e.g. between the SELECT and the emit, another writer
    raced it in), ``_emit_operator_escalation`` reads the dedup row and
    no-ops without persisting a second event."""
    task_id = uuid.uuid4()
    ready_at = datetime.now(timezone.utc) - STEP_STARVATION_THRESHOLD - timedelta(minutes=1)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_Row(task_id, "example/repo", "analyze", "role-architect", ready_at)]),
        _dedup_probe(existing=True),  # an existing escalation row
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await run_step_starvation_sweep(session, dispatcher)

    dispatcher.persist_and_publish.assert_not_awaited()


# ── multi-step ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_stalled_steps_each_escalated_once() -> None:
    """SQL returns N stalled rows → N escalations emitted in one tick."""
    now = datetime.now(timezone.utc)
    ready_at = now - STEP_STARVATION_THRESHOLD - timedelta(minutes=5)
    rows = [
        _Row(
            uuid.uuid4(),
            "example/repo",
            f"step-{i}",
            "role-test",
            ready_at,
        )
        for i in range(2)
    ]
    session = AsyncMock()
    # One sweep SELECT + one dedup SELECT per row.
    session.execute = AsyncMock(side_effect=[
        _IterableResult(rows),
        _dedup_probe(existing=False),
        _dedup_probe(existing=False),
    ])
    session.get = AsyncMock(side_effect=[_fake_task(r.task_id) for r in rows])

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    escalated = await run_step_starvation_sweep(session, dispatcher, now=now)

    assert escalated == 2
    assert dispatcher.persist_and_publish.await_count == 2


# ── handle_scheduled_tick wiring ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_scheduled_tick_routes_sweep_to_deterministic_path() -> None:
    """A scheduled tick for ``wf-step-starvation-sweep`` runs the deterministic
    sweep — it does NOT call the synthetic-task dispatch helper (ADR-0057)
    and therefore does NOT look up a ``WorkflowVersion``. Returns ``None``
    because the sweep materializes no run."""
    from treadmill_api.coordination.triggers import handle_scheduled_tick
    from treadmill_api.events.schedule import ScheduledTick

    schedule_id = uuid.uuid4()
    mock_schedule = MagicMock()
    mock_schedule.status = "active"
    mock_schedule.workflow_id = STEP_STARVATION_SWEEP_WORKFLOW_ID

    session = AsyncMock()
    session.get = AsyncMock(return_value=mock_schedule)
    # If the deterministic intercept fires, session.execute MUST NOT be
    # called for the WorkflowVersion lookup. We assert that below.
    session.execute = AsyncMock()

    typed = ScheduledTick(
        schedule_id=schedule_id,
        workflow_id=STEP_STARVATION_SWEEP_WORKFLOW_ID,
        rendered_payload={},
    )

    with patch.object(
        sweep_mod, "run_step_starvation_sweep", new=AsyncMock(return_value=0),
    ) as mocked_sweep:
        result = await handle_scheduled_tick(
            session, dispatcher=MagicMock(), typed=typed,
        )

    assert result is None
    mocked_sweep.assert_awaited_once()
    # The deterministic intercept must short-circuit before the
    # _dispatch_via_synthetic_task path issues its WorkflowVersion SELECT.
    session.execute.assert_not_awaited()


# ── threshold honored ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_passes_threshold_cutoff_to_sql() -> None:
    """The sweep computes ``now - STEP_STARVATION_THRESHOLD`` and binds it as
    the SQL ``:cutoff`` parameter — a regression guard for accidentally
    inverting the comparison or hard-coding a stale cutoff."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    captured: dict[str, Any] = {}

    async def _capture_execute(stmt: Any, params: Any = None) -> Any:
        captured["params"] = params
        return _IterableResult([])

    session = MagicMock()
    session.execute = _capture_execute

    await run_step_starvation_sweep(session, MagicMock(), now=now)

    assert captured["params"] == {"cutoff": now - STEP_STARVATION_THRESHOLD}
