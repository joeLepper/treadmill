"""Scheduler policy functions — ported from RAMJAC's scrape_scheduler.

All four functions are deterministic and side-effect-free so they are easy
to unit-test in isolation. The runner calls them but they carry no runner
state.

RAMJAC originals (commit 2b9e9cead^, RAMJAC PR #489):
  calculate_jitter_seconds  — sha1-based deterministic jitter
  is_quiet                  — hour-of-day window with midnight wraparound
  quiet_window_end_epoch    — epoch when the current quiet window closes
  next_interval_seconds     — exponential backoff with hard cap + jitter
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def calculate_jitter_seconds(key: str, amplitude: int) -> int:
    """Deterministic jitter offset in the closed interval ``[-amplitude, +amplitude]``.

    Uses SHA-1 of ``key`` so the same schedule always gets the same offset
    across runner restarts — fire times are stable, not random.
    Amplitude of 0 always returns 0.
    """
    if amplitude == 0:
        return 0
    digest = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    span = 2 * amplitude + 1
    return digest % span - amplitude


def _parse_quiet_hours(quiet_hours: str) -> tuple[int, int]:
    """Parse ``"HH-HH"`` → ``(start_hour, end_hour)`` as ints."""
    start_str, end_str = quiet_hours.split("-", 1)
    return int(start_str), int(end_str)


def is_quiet(dt: datetime, quiet_hours: str, quiet_tz: str) -> bool:
    """Return ``True`` iff ``dt`` falls inside the quiet window.

    ``quiet_hours`` is in ``"HH-HH"`` format (e.g. ``"20-4"`` = 8 pm–4 am).
    Handles wraparound windows where ``start_hour > end_hour``.

    RAMJAC wraparound rule: the window ``"20-4"`` covers hours
    20, 21, 22, 23, 0, 1, 2, 3 (end is exclusive).
    """
    tz = ZoneInfo(quiet_tz)
    hour = dt.astimezone(tz).hour
    start_h, end_h = _parse_quiet_hours(quiet_hours)
    if start_h <= end_h:
        return start_h <= hour < end_h
    # Wraparound: e.g. "20-4" → (hour >= 20) OR (hour < 4)
    return hour >= start_h or hour < end_h


def quiet_window_end_epoch(dt: datetime, quiet_hours: str, quiet_tz: str) -> float:
    """Epoch seconds at which the current quiet window closes.

    Assumes ``dt`` is already inside a quiet window. If the end-hour has
    already passed today in ``quiet_tz``, the result is tomorrow's end-hour
    (handles overnight windows such as ``"20-4"``).
    """
    tz = ZoneInfo(quiet_tz)
    local_dt = dt.astimezone(tz)
    _, end_h = _parse_quiet_hours(quiet_hours)
    candidate = local_dt.replace(hour=end_h, minute=0, second=0, microsecond=0)
    if candidate <= local_dt:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def next_interval_seconds(
    base: float,
    factor: float,
    attempts: int,
    cap: float,
    jitter_seconds: int = 0,
) -> int:
    """Exponential-backoff interval: ``min(cap, base × factor^attempts) + jitter``.

    Used for retry / quiet-hour backoff scheduling. The result is always a
    non-negative integer (truncated, not rounded).
    """
    interval = min(cap, base * (factor**attempts))
    return int(interval) + jitter_seconds
