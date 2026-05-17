"""Unit tests for scheduler/runner.py.

Uses stub sessionmakers and publishers so no database or event bus is
required. Tests verify the gate logic (fire vs skip) and the missed-tick
replay path.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.scheduler.runner import SchedulerRunner, _ref_time


# ── helpers ───────────────────────────────────────────────────────────────────


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _make_schedule(
    *,
    cron_expression: str = "0 * * * *",
    jitter_seconds: int = 0,
    quiet_hours: str | None = None,
    quiet_tz: str = "America/Los_Angeles",
    last_fired_at: datetime | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.cron_expression = cron_expression
    s.jitter_seconds = jitter_seconds
    s.quiet_hours = quiet_hours
    s.quiet_tz = quiet_tz
    s.last_fired_at = last_fired_at
    s.created_at = created_at or _utc(2026, 1, 15, 8, 0)
    s.status = "active"
    s.workflow_id = "wf-test"
    s.payload_template = {}
    return s


def _make_session(schedules: list) -> MagicMock:
    """Return an AsyncMock session whose execute().scalars().all() gives schedules."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = schedules
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


def _make_sessionmaker(schedules: list) -> MagicMock:
    """Sessionmaker whose both () and .begin() context managers yield a stub session."""
    session = _make_session(schedules)
    sm = MagicMock()

    @asynccontextmanager
    async def _ctx():
        yield session

    @asynccontextmanager
    async def _begin_ctx():
        yield session

    sm.return_value = _ctx()
    sm.begin = MagicMock(return_value=_begin_ctx())
    return sm


def _make_publisher() -> AsyncMock:
    pub = AsyncMock()
    pub.publish = AsyncMock()
    return pub


# ── _ref_time ─────────────────────────────────────────────────────────────────


def test_ref_time_prefers_last_fired_at():
    s = _make_schedule(
        last_fired_at=_utc(2026, 1, 15, 10, 0),
        created_at=_utc(2026, 1, 15, 8, 0),
    )
    assert _ref_time(s) == _utc(2026, 1, 15, 10, 0)


def test_ref_time_falls_back_to_created_at():
    s = _make_schedule(last_fired_at=None, created_at=_utc(2026, 1, 15, 8, 0))
    assert _ref_time(s) == _utc(2026, 1, 15, 8, 0)


def test_ref_time_adds_utc_to_naive():
    naive = datetime(2026, 1, 15, 8, 0, 0)
    s = _make_schedule(last_fired_at=naive)
    result = _ref_time(s)
    assert result.tzinfo is not None


# ── _maybe_fire gate logic ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maybe_fire_skips_when_not_due():
    # last_fired_at = 10:00, hourly cron → next fire is 11:00; now = 10:30
    schedule = _make_schedule(
        cron_expression="0 * * * *",
        last_fired_at=_utc(2026, 1, 15, 10, 0),
    )
    now = _utc(2026, 1, 15, 10, 30)

    publisher = _make_publisher()
    sm = _make_sessionmaker([schedule])
    runner = SchedulerRunner(sm, publisher=publisher)

    await runner._maybe_fire(schedule, now, publisher)

    publisher.publish.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_fire_fires_when_due():
    # last_fired_at = 10:00, hourly cron → next fire is 11:00; now = 11:01
    schedule = _make_schedule(
        cron_expression="0 * * * *",
        last_fired_at=_utc(2026, 1, 15, 10, 0),
    )
    now = _utc(2026, 1, 15, 11, 1)

    publisher = _make_publisher()
    sm = MagicMock()

    @asynccontextmanager
    async def _begin():
        session = AsyncMock()
        session.add = MagicMock()
        session.execute = AsyncMock()
        yield session

    sm.begin = MagicMock(return_value=_begin())
    runner = SchedulerRunner(sm, publisher=publisher)

    await runner._maybe_fire(schedule, now, publisher)

    publisher.publish.assert_called_once()


@pytest.mark.asyncio
async def test_maybe_fire_skips_during_quiet_hours():
    # Schedule fires at 10:00; now = 11:01 but inside quiet window "9-17" PST.
    # 11:01 UTC = 03:01 PST (UTC-8), which is NOT in "9-17" PST.
    # Use "0-23" to cover all hours and guarantee quiet.
    schedule = _make_schedule(
        cron_expression="0 * * * *",
        last_fired_at=_utc(2026, 1, 15, 10, 0),
        quiet_hours="0-23",
        quiet_tz="UTC",
    )
    now = _utc(2026, 1, 15, 11, 1)

    publisher = _make_publisher()
    sm = MagicMock()
    runner = SchedulerRunner(sm, publisher=publisher)

    await runner._maybe_fire(schedule, now, publisher)

    publisher.publish.assert_not_called()


# ── missed-tick replay ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_fires_missed_ticks():
    # Schedule last fired at 8:00; hourly; now is 12:01 → 4 missed ticks.
    schedule = _make_schedule(
        cron_expression="0 * * * *",
        last_fired_at=_utc(2026, 1, 15, 8, 0),
    )
    now = _utc(2026, 1, 15, 12, 1)
    window_start = now - timedelta(hours=4)

    publisher = _make_publisher()
    fire_calls = []

    runner = SchedulerRunner(MagicMock(), publisher=publisher)

    async def _fake_fire(sched, fire_at, pub):
        fire_calls.append(fire_at)

    runner._fire = _fake_fire  # type: ignore[method-assign]

    await runner._replay_schedule(schedule, window_start, now, publisher)

    # Should have fired at 9, 10, 11, 12
    assert len(fire_calls) == 4


@pytest.mark.asyncio
async def test_replay_skips_ticks_before_window():
    # Schedule last fired at 6:00; window is [8:00, 12:01).
    # Hourly ticks at 7:00 are before window → only 8–12 replayed.
    schedule = _make_schedule(
        cron_expression="0 * * * *",
        last_fired_at=_utc(2026, 1, 15, 6, 0),
    )
    now = _utc(2026, 1, 15, 12, 1)
    window_start = _utc(2026, 1, 15, 8, 0)

    publisher = _make_publisher()
    fire_calls = []

    runner = SchedulerRunner(MagicMock(), publisher=publisher)

    async def _fake_fire(sched, fire_at, pub):
        fire_calls.append(fire_at)

    runner._fire = _fake_fire  # type: ignore[method-assign]

    await runner._replay_schedule(schedule, window_start, now, publisher)

    # 9, 10, 11, 12 — starts at search_start=max(6:00, 8:00)=8:00 → first yield is 9:00
    assert len(fire_calls) == 4
    assert fire_calls[0] == _utc(2026, 1, 15, 9, 0)


@pytest.mark.asyncio
async def test_replay_skips_quiet_ticks():
    # All hours are quiet → nothing should replay
    schedule = _make_schedule(
        cron_expression="0 * * * *",
        last_fired_at=_utc(2026, 1, 15, 8, 0),
        quiet_hours="0-23",
        quiet_tz="UTC",
    )
    now = _utc(2026, 1, 15, 12, 1)
    window_start = _utc(2026, 1, 15, 8, 0)

    publisher = _make_publisher()
    fire_calls = []

    runner = SchedulerRunner(MagicMock(), publisher=publisher)

    async def _fake_fire(sched, fire_at, pub):
        fire_calls.append(fire_at)

    runner._fire = _fake_fire  # type: ignore[method-assign]

    await runner._replay_schedule(schedule, window_start, now, publisher)

    assert fire_calls == []
