"""Unit tests for the Autoscaler tick logic and ScalableTarget bounds parser."""

from __future__ import annotations

import pytest

from treadmill_local.autoscaler import Autoscaler, parse_scalable_target_bounds


class _Fake:
    """Test double exposing the three callables Autoscaler depends on, plus
    a mutable depth + worker count and a counter of starts."""

    def __init__(self, depth: int = 0, current: int = 0):
        self.depth = depth
        self.current = current
        self.starts = 0

    def queue_depth(self) -> int:
        return self.depth

    def worker_count(self) -> int:
        return self.current

    def start_worker(self) -> None:
        self.starts += 1
        # Simulate the worker becoming visible to docker ps.
        self.current += 1


def _autoscaler(fake: _Fake, *, min_count: int = 0, max_count: int = 1) -> Autoscaler:
    return Autoscaler(
        queue_depth_fn=fake.queue_depth,
        worker_count_fn=fake.worker_count,
        start_worker_fn=fake.start_worker,
        min_count=min_count,
        max_count=max_count,
        tick_seconds=0.0,
    )


# ── tick logic ────────────────────────────────────────────────────────────────


def test_tick_starts_worker_when_depth_exceeds_current():
    fake = _Fake(depth=1, current=0)
    a = _autoscaler(fake)
    snap = a.tick()
    assert snap.depth == 1
    assert snap.desired == 1
    assert snap.started == 1
    assert fake.starts == 1


def test_tick_caps_starts_at_max():
    fake = _Fake(depth=10, current=0)
    a = _autoscaler(fake, max_count=3)
    snap = a.tick()
    assert snap.desired == 3
    assert snap.started == 3
    assert fake.starts == 3


def test_tick_does_not_start_when_desired_le_current():
    fake = _Fake(depth=5, current=2)
    a = _autoscaler(fake, max_count=2)
    snap = a.tick()
    assert snap.desired == 2
    assert snap.started == 0
    assert fake.starts == 0


def test_tick_zero_depth_zero_workers():
    fake = _Fake(depth=0, current=0)
    a = _autoscaler(fake)
    snap = a.tick()
    assert snap.desired == 0
    assert snap.started == 0


def test_tick_respects_min_count():
    """If min=2 and depth=0, desired floors at 2 — so the loop will start
    workers up to the minimum even with no work."""
    fake = _Fake(depth=0, current=0)
    a = _autoscaler(fake, min_count=2, max_count=5)
    snap = a.tick()
    assert snap.desired == 2
    assert snap.started == 2


def test_tick_natural_drain_when_depth_drops():
    """Workers exit after each step. When depth falls, the loop simply does
    not start replacements; current decays naturally as workers finish."""
    fake = _Fake(depth=3, current=1)  # one worker already running
    a = _autoscaler(fake, max_count=1)
    snap = a.tick()
    assert snap.desired == 1
    assert snap.started == 0  # already at max

    # Worker finishes and exits — outside the autoscaler's control.
    fake.current = 0
    fake.depth = 0
    snap = a.tick()
    assert snap.desired == 0
    assert snap.started == 0


# ── invariants on construction ────────────────────────────────────────────────


def test_constructor_rejects_negative_min():
    with pytest.raises(ValueError, match="min_count must be >= 0"):
        _autoscaler(_Fake(), min_count=-1)


def test_constructor_rejects_max_below_min():
    with pytest.raises(ValueError, match="must be >= min_count"):
        _autoscaler(_Fake(), min_count=5, max_count=3)


# ── ScalableTarget bounds parsing ─────────────────────────────────────────────


def test_parse_bounds_present():
    assert parse_scalable_target_bounds({"MinCapacity": 0, "MaxCapacity": 4}) == (0, 4)


def test_parse_bounds_missing_use_defaults():
    assert parse_scalable_target_bounds({}) == (0, 1)


def test_parse_bounds_partial():
    assert parse_scalable_target_bounds({"MaxCapacity": 7}) == (0, 7)
    assert parse_scalable_target_bounds({"MinCapacity": 2}) == (2, 1)
    # ^ Note: that's (2, 1), the parser does not enforce min<=max — Autoscaler
    # constructor catches that and raises.


def test_parse_bounds_rejects_non_int():
    with pytest.raises(TypeError):
        parse_scalable_target_bounds({"MinCapacity": "0", "MaxCapacity": 1})
