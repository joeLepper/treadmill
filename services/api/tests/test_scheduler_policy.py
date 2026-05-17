"""Unit tests for scheduler/policy.py — ported from RAMJAC's scheduler.

Covers RAMJAC's exact behaviour including the midnight-wraparound quiet
window and the quiet-multiplier hard cap. No database or event bus required.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from treadmill_api.scheduler.policy import (
    calculate_jitter_seconds,
    is_quiet,
    next_interval_seconds,
    quiet_window_end_epoch,
)

# ── calculate_jitter_seconds ─────────────────────────────────────────────────


def test_jitter_is_deterministic():
    a = calculate_jitter_seconds("sched-abc", 60)
    b = calculate_jitter_seconds("sched-abc", 60)
    assert a == b


def test_jitter_different_keys_differ():
    a = calculate_jitter_seconds("sched-abc", 60)
    b = calculate_jitter_seconds("sched-xyz", 60)
    assert a != b


def test_jitter_within_bounds():
    for key in ["key-1", "key-2", "key-3", "key-99"]:
        for amp in [1, 30, 60, 300]:
            result = calculate_jitter_seconds(key, amp)
            assert -amp <= result <= amp, f"key={key!r} amp={amp} result={result}"


def test_jitter_zero_amplitude_always_zero():
    assert calculate_jitter_seconds("any-key", 0) == 0
    assert calculate_jitter_seconds("other-key", 0) == 0


def test_jitter_matches_sha1_formula():
    key = "schedule-deadbeef"
    amplitude = 60
    digest = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    expected = digest % (2 * amplitude + 1) - amplitude
    assert calculate_jitter_seconds(key, amplitude) == expected


# ── is_quiet ─────────────────────────────────────────────────────────────────

TZ = "America/Los_Angeles"


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 15, hour, minute, 0, tzinfo=timezone.utc)


def _pst_hour(hour: int) -> datetime:
    """Return a UTC datetime corresponding to the given PST hour on 2026-01-15.
    PST = UTC-8.
    """
    utc_hour = (hour + 8) % 24
    day_offset = (hour + 8) // 24
    return datetime(2026, 1, 15 + day_offset, utc_hour, 0, 0, tzinfo=timezone.utc)


def test_is_quiet_simple_window_inside():
    # "9-17" = 9 am to 5 pm, no wraparound
    dt = _pst_hour(10)  # 10 am PST
    assert is_quiet(dt, "9-17", TZ) is True


def test_is_quiet_simple_window_outside():
    dt = _pst_hour(18)  # 6 pm PST
    assert is_quiet(dt, "9-17", TZ) is False


def test_is_quiet_simple_window_at_start_is_quiet():
    dt = _pst_hour(9)  # exactly 9 am PST
    assert is_quiet(dt, "9-17", TZ) is True


def test_is_quiet_simple_window_at_end_is_not_quiet():
    dt = _pst_hour(17)  # exactly 5 pm PST — end is exclusive
    assert is_quiet(dt, "9-17", TZ) is False


def test_is_quiet_wraparound_inside_evening():
    # RAMJAC wraparound case: "20-4" = 8 pm to 4 am
    dt = _pst_hour(21)  # 9 pm PST — inside
    assert is_quiet(dt, "20-4", TZ) is True


def test_is_quiet_wraparound_inside_early_morning():
    dt = _pst_hour(3)  # 3 am PST — inside (past midnight part)
    assert is_quiet(dt, "20-4", TZ) is True


def test_is_quiet_wraparound_outside_daytime():
    dt = _pst_hour(10)  # 10 am PST — outside
    assert is_quiet(dt, "20-4", TZ) is False


def test_is_quiet_wraparound_at_start():
    dt = _pst_hour(20)  # 8 pm PST — at start, inclusive
    assert is_quiet(dt, "20-4", TZ) is True


def test_is_quiet_wraparound_at_end_is_not_quiet():
    dt = _pst_hour(4)  # 4 am PST — end is exclusive
    assert is_quiet(dt, "20-4", TZ) is False


def test_is_quiet_midnight_itself_is_inside_overnight_window():
    dt = _pst_hour(0)  # midnight PST
    assert is_quiet(dt, "20-4", TZ) is True


# ── quiet_window_end_epoch ───────────────────────────────────────────────────


def test_quiet_window_end_epoch_same_day():
    # "9-17" — currently 10 am PST; end is today at 5 pm PST
    dt = _pst_hour(10)
    end = quiet_window_end_epoch(dt, "9-17", TZ)
    expected = _pst_hour(17).timestamp()
    assert abs(end - expected) < 2  # within 2 s of floating-point rounding


def test_quiet_window_end_epoch_overnight_window():
    # "20-4" — currently 10 pm PST; end is tomorrow at 4 am PST
    dt = _pst_hour(22)
    end = quiet_window_end_epoch(dt, "20-4", TZ)
    # 4 am PST the following day
    expected = _pst_hour(4 + 24).timestamp()
    assert abs(end - expected) < 2


def test_quiet_window_end_epoch_early_morning_before_end():
    # "20-4" — currently 2 am PST; end is today at 4 am PST (same calendar day)
    dt = _pst_hour(2)
    end = quiet_window_end_epoch(dt, "20-4", TZ)
    expected = _pst_hour(4).timestamp()
    assert abs(end - expected) < 2


def test_quiet_window_end_epoch_is_in_the_future():
    dt = datetime.now(tz=timezone.utc)
    end = quiet_window_end_epoch(dt, "0-23", TZ)
    assert end > dt.timestamp()


# ── next_interval_seconds ────────────────────────────────────────────────────


def test_next_interval_exponential_growth():
    # base=1, factor=2: 0 attempts→1, 1 attempt→2, 2→4, 3→8
    assert next_interval_seconds(1, 2, 0, 3600) == 1
    assert next_interval_seconds(1, 2, 1, 3600) == 2
    assert next_interval_seconds(1, 2, 2, 3600) == 4
    assert next_interval_seconds(1, 2, 3, 3600) == 8


def test_next_interval_capped_at_max():
    # With a cap of 10, any attempt beyond log_2(10) returns 10
    result = next_interval_seconds(1, 2, 10, 10)
    assert result == 10


def test_next_interval_quiet_multiplier_cap():
    # RAMJAC quiet-hours cap: during quiet hours the interval is
    # min(quiet_max_seconds, base_interval * quiet_multiplier).
    # Model this as next_interval_seconds(base * multiplier, 1, 0, quiet_max_seconds).
    base_interval = 3600.0  # 1-hour cron → 1 h base
    multiplier = 6.0
    quiet_max = 43200  # 12 h
    # base * multiplier = 21600 < 43200 → not capped
    result = next_interval_seconds(base_interval * multiplier, 1.0, 0, quiet_max)
    assert result == 21600

    # With a small cap (3600) it IS capped
    result_capped = next_interval_seconds(base_interval * multiplier, 1.0, 0, 3600)
    assert result_capped == 3600


def test_next_interval_jitter_added():
    result = next_interval_seconds(60, 1, 0, 3600, jitter_seconds=15)
    assert result == 75


def test_next_interval_zero_attempts():
    assert next_interval_seconds(30, 2, 0, 3600) == 30
