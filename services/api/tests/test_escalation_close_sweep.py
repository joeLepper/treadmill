"""Unit tests for the deterministic escalation-close sweep (ADR-0062 Step 2).

Mirrors ``test_stuck_task_sweep.py``'s shape — pure unit tests with mocked
session/dispatcher, no DB, no live LLM. Covers one happy path per close
trigger (re_progressed / pr_merged / cancelled / superseded), the
no-op case (open incident with no trigger fires), the operator-close
emission path (Step 3 will call this from the CLI), the priority
ordering of the trigger checks, and the ``handle_scheduled_tick`` routing
seam that intercepts the ``wf-escalation-close-sweep`` slug.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination import escalation_close_sweep as sweep_mod
from treadmill_api.coordination.escalation_close_sweep import (
    ESCALATION_CLOSE_SWEEP_WORKFLOW_ID,
    emit_operator_close,
    run_escalation_close_sweep,
)


# ── Test fixtures ────────────────────────────────────────────────────────────


class _OpenRow:
    """A canned row matching the open-incidents SELECT shape."""

    def __init__(self, task_id: uuid.UUID, opened_at: datetime) -> None:
        self.task_id = task_id
        self.opened_at = opened_at


class _IterableResult:
    """Mimics the SQLAlchemy ``Result`` shape the sweep iterates."""

    def __init__(self, rows: list[_OpenRow]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)


def _trigger_hit() -> MagicMock:
    """A trigger-probe result whose ``.first()`` returns a row (hit)."""
    r = MagicMock()
    r.first.return_value = MagicMock()
    return r


def _trigger_miss() -> MagicMock:
    """A trigger-probe result whose ``.first()`` returns ``None`` (miss)."""
    r = MagicMock()
    r.first.return_value = None
    return r


def _fake_task(task_id: uuid.UUID) -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.plan_id = uuid.uuid4()
    return t


def _opened_at(minutes_ago: int = 10) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


# ── Close trigger: re_progressed ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_re_progressed_closes_incident() -> None:
    """A ``step.completed`` after ``opened_at`` closes the incident with
    ``close_reason='re_progressed'`` — the first-priority trigger."""
    task_id = uuid.uuid4()
    opened_at = _opened_at(minutes_ago=15)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_OpenRow(task_id, opened_at)]),
        _trigger_hit(),  # re_progressed probe fires
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    closed = await run_escalation_close_sweep(session, dispatcher)

    assert closed == 1
    dispatcher.persist_and_publish.assert_awaited_once()
    kwargs = dispatcher.persist_and_publish.await_args.kwargs
    assert kwargs["entity_type"] == "task"
    assert kwargs["action"] == "escalation_closed"
    payload = kwargs["payload"]
    assert payload.close_reason == "re_progressed"
    assert payload.opened_at == opened_at
    assert payload.mttr_seconds >= 0
    # 15 min back, within a few seconds — assert a sane lower bound.
    assert payload.mttr_seconds >= 14 * 60


# ── Close trigger: pr_merged ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_merged_closes_incident() -> None:
    """A ``github.pr_merged`` event closes the incident with
    ``close_reason='pr_merged'`` when ``re_progressed`` does not fire."""
    task_id = uuid.uuid4()
    opened_at = _opened_at(minutes_ago=5)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_OpenRow(task_id, opened_at)]),
        _trigger_miss(),  # re_progressed misses
        _trigger_hit(),   # pr_merged hits
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    closed = await run_escalation_close_sweep(session, dispatcher)

    assert closed == 1
    payload = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    assert payload.close_reason == "pr_merged"


# ── Close trigger: cancelled ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelled_closes_incident() -> None:
    """A ``task.cancelled`` event closes the incident with
    ``close_reason='cancelled'`` when the two higher-priority triggers
    do not fire."""
    task_id = uuid.uuid4()
    opened_at = _opened_at(minutes_ago=3)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_OpenRow(task_id, opened_at)]),
        _trigger_miss(),  # re_progressed misses
        _trigger_miss(),  # pr_merged misses
        _trigger_hit(),   # cancelled hits
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    closed = await run_escalation_close_sweep(session, dispatcher)

    assert closed == 1
    payload = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    assert payload.close_reason == "cancelled"


# ── Close trigger: superseded ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_superseded_closes_incident() -> None:
    """A ``task.superseded`` event closes the incident with
    ``close_reason='superseded'`` — the last sweep-detectable trigger."""
    task_id = uuid.uuid4()
    opened_at = _opened_at(minutes_ago=8)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_OpenRow(task_id, opened_at)]),
        _trigger_miss(),  # re_progressed misses
        _trigger_miss(),  # pr_merged misses
        _trigger_miss(),  # cancelled misses
        _trigger_hit(),   # superseded hits
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    closed = await run_escalation_close_sweep(session, dispatcher)

    assert closed == 1
    payload = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    assert payload.close_reason == "superseded"


# ── No-op: open incident with no trigger fires ───────────────────────────────


@pytest.mark.asyncio
async def test_open_incident_no_trigger_is_noop() -> None:
    """An open incident whose underlying task has hit none of the four
    sweep-detectable close triggers stays open — the sweep emits
    nothing for it."""
    task_id = uuid.uuid4()
    opened_at = _opened_at(minutes_ago=2)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_OpenRow(task_id, opened_at)]),
        _trigger_miss(),  # re_progressed misses
        _trigger_miss(),  # pr_merged misses
        _trigger_miss(),  # cancelled misses
        _trigger_miss(),  # superseded misses
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    closed = await run_escalation_close_sweep(session, dispatcher)

    assert closed == 0
    dispatcher.persist_and_publish.assert_not_awaited()


# ── No-op: no open incidents at all ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_open_incidents_is_clean_noop() -> None:
    """The open-incidents SELECT returning zero rows short-circuits
    before any close-trigger probe — no per-incident SQL, no emit."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterableResult([]))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    closed = await run_escalation_close_sweep(session, dispatcher)

    assert closed == 0
    dispatcher.persist_and_publish.assert_not_awaited()
    # Exactly one SELECT (the open-incidents query); no trigger probes.
    assert session.execute.await_count == 1


# ── Priority ordering: re_progressed wins when both could match ──────────────


@pytest.mark.asyncio
async def test_re_progressed_wins_priority_over_terminal_triggers() -> None:
    """When ``re_progressed`` AND a terminal trigger could both apply,
    ``re_progressed`` wins — the sweep stops at the first match. We
    assert this by verifying only the first trigger probe is executed
    after the open-incidents SELECT."""
    task_id = uuid.uuid4()
    opened_at = _opened_at(minutes_ago=10)

    session = AsyncMock()
    # Open incidents + ONE trigger probe. If the sweep called more than
    # one probe per incident, the AsyncMock would raise StopIteration.
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_OpenRow(task_id, opened_at)]),
        _trigger_hit(),  # re_progressed hits — should stop here
    ])
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await run_escalation_close_sweep(session, dispatcher)

    # 1 open-incidents SELECT + 1 trigger probe + 0 more = 2 awaits.
    assert session.execute.await_count == 2


# ── Operator close path (Step 3 CLI emission) ────────────────────────────────


@pytest.mark.asyncio
async def test_emit_operator_close_emits_close_event() -> None:
    """``emit_operator_close`` writes a ``task.escalation_closed`` with
    ``close_reason='operator_close'`` — the CLI's ``escalations close``
    command (Step 3) calls this directly so the sweep and the CLI share
    one emitter."""
    task_id = uuid.uuid4()
    opened_at = _opened_at(minutes_ago=30)

    session = AsyncMock()
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await emit_operator_close(
        session,
        dispatcher,
        task_id=task_id,
        opened_at=opened_at,
    )

    dispatcher.persist_and_publish.assert_awaited_once()
    payload = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    assert payload.close_reason == "operator_close"
    assert payload.opened_at == opened_at
    # 30 min back, within a few seconds.
    assert payload.mttr_seconds >= 29 * 60


# ── MTTR computation honored ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mttr_seconds_uses_total_seconds_across_days() -> None:
    """``mttr_seconds`` must use ``total_seconds()`` (not ``.seconds``)
    so multi-day incidents report the real duration. A 26-hour-old
    incident regressed to ``.seconds`` would report 2 h.
    """
    task_id = uuid.uuid4()
    opened_at = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 6, 2, 14, 0, 0, tzinfo=timezone.utc)  # +26 h

    session = AsyncMock()
    session.get = AsyncMock(return_value=_fake_task(task_id))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    await emit_operator_close(
        session,
        dispatcher,
        task_id=task_id,
        opened_at=opened_at,
        now=now,
    )

    payload = dispatcher.persist_and_publish.await_args.kwargs["payload"]
    # 26 hours = 93600 seconds. ``.seconds`` would be 2 * 3600 = 7200.
    assert payload.mttr_seconds == 26 * 3600


# ── Multi-incident: each one detected + emitted ──────────────────────────────


@pytest.mark.asyncio
async def test_multiple_open_incidents_each_get_one_close() -> None:
    """SQL returns N open incidents; each one with a fired trigger
    receives one close event — symmetric to the stuck-sweep's
    multi-task path."""
    task_ids = [uuid.uuid4() for _ in range(3)]
    opened_at = _opened_at(minutes_ago=5)

    session = AsyncMock()
    # 1 open SELECT + 3 incidents * 1 trigger probe each (all re_progressed hits).
    session.execute = AsyncMock(side_effect=[
        _IterableResult([_OpenRow(tid, opened_at) for tid in task_ids]),
        _trigger_hit(),
        _trigger_hit(),
        _trigger_hit(),
    ])
    session.get = AsyncMock(side_effect=[_fake_task(tid) for tid in task_ids])

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    closed = await run_escalation_close_sweep(session, dispatcher)

    assert closed == 3
    assert dispatcher.persist_and_publish.await_count == 3


# ── handle_scheduled_tick routing ────────────────────────────────────────────
