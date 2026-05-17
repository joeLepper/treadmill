"""Unit tests for scheduler/cron.py — croniter wrapper.

No database required.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from treadmill_api.scheduler.cron import iter_fires, next_fire_time


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ── next_fire_time ────────────────────────────────────────────────────────────


def test_next_fire_time_hourly():
    # "0 * * * *" fires at the top of each hour
    after = _utc(2026, 1, 15, 10, 30)
    result = next_fire_time("0 * * * *", after)
    assert result == _utc(2026, 1, 15, 11, 0)


def test_next_fire_time_daily_at_noon():
    # "0 12 * * *" fires at noon each day
    after = _utc(2026, 1, 15, 9, 0)
    result = next_fire_time("0 12 * * *", after)
    assert result == _utc(2026, 1, 15, 12, 0)


def test_next_fire_time_past_midnight():
    # After 23:30, next hourly fire is at 00:00 next day
    after = _utc(2026, 1, 15, 23, 30)
    result = next_fire_time("0 * * * *", after)
    assert result == _utc(2026, 1, 16, 0, 0)


def test_next_fire_time_is_strictly_after():
    # If after is exactly on a fire time, the result must be the NEXT one
    on_the_hour = _utc(2026, 1, 15, 10, 0)
    result = next_fire_time("0 * * * *", on_the_hour)
    assert result == _utc(2026, 1, 15, 11, 0)


# ── iter_fires ────────────────────────────────────────────────────────────────


def test_iter_fires_hourly_four_hours():
    start = _utc(2026, 1, 15, 8, 0)
    end = _utc(2026, 1, 15, 12, 0)
    fires = list(iter_fires("0 * * * *", start, end))
    assert fires == [
        _utc(2026, 1, 15, 9, 0),
        _utc(2026, 1, 15, 10, 0),
        _utc(2026, 1, 15, 11, 0),
    ]


def test_iter_fires_end_is_exclusive():
    start = _utc(2026, 1, 15, 8, 0)
    end = _utc(2026, 1, 15, 11, 0)  # exactly on an hourly mark
    fires = list(iter_fires("0 * * * *", start, end))
    # 11:00 is the end → excluded
    assert _utc(2026, 1, 15, 11, 0) not in fires
    assert fires[-1] == _utc(2026, 1, 15, 10, 0)


def test_iter_fires_empty_when_start_equals_end():
    t = _utc(2026, 1, 15, 10, 0)
    assert list(iter_fires("0 * * * *", t, t)) == []


def test_iter_fires_empty_when_start_after_end():
    start = _utc(2026, 1, 15, 12, 0)
    end = _utc(2026, 1, 15, 8, 0)
    assert list(iter_fires("0 * * * *", start, end)) == []


def test_iter_fires_no_fire_in_range():
    # "0 9 * * 1" = 9 am on Mondays; range is within a non-Monday day
    start = _utc(2026, 1, 15, 0, 0)  # Thursday
    end = _utc(2026, 1, 15, 23, 59)
    assert list(iter_fires("0 9 * * 1", start, end)) == []


def test_iter_fires_missed_ticks_4h_window():
    # Simulate 4 h of downtime for an hourly schedule
    start = _utc(2026, 1, 15, 8, 0)   # last_fired_at
    end = _utc(2026, 1, 15, 12, 30)   # now
    fires = list(iter_fires("0 * * * *", start, end))
    assert len(fires) == 4
    assert fires[0] == _utc(2026, 1, 15, 9, 0)
    assert fires[-1] == _utc(2026, 1, 15, 12, 0)
