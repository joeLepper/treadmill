"""Unit tests for the Autoscaler tick logic and ScalableTarget bounds parser."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from treadmill_local.autoscaler import (
    Autoscaler,
    _container_age_seconds,
    _REAP_AGE_SECONDS,
    parse_scalable_target_bounds,
)


class _Fake:
    """Test double exposing the four callables Autoscaler depends on, plus
    a mutable depth + worker count and counters of starts and reaps."""

    def __init__(self, depth: int = 0, current: int = 0, reap_return: int = 0):
        self.depth = depth
        self.current = current
        self.starts = 0
        self.reap_calls = 0
        self.reap_return = reap_return

    def queue_depth(self) -> int:
        return self.depth

    def worker_count(self) -> int:
        return self.current

    def start_worker(self) -> None:
        self.starts += 1
        # Simulate the worker becoming visible to docker ps.
        self.current += 1

    def reap_dead_workers(self) -> int:
        self.reap_calls += 1
        return self.reap_return


def _autoscaler(
    fake: _Fake,
    *,
    min_count: int = 0,
    max_count: int = 1,
    with_reap: bool = False,
) -> Autoscaler:
    return Autoscaler(
        queue_depth_fn=fake.queue_depth,
        worker_count_fn=fake.worker_count,
        start_worker_fn=fake.start_worker,
        reap_dead_workers_fn=fake.reap_dead_workers if with_reap else None,
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


# ── reap-on-tick integration with Autoscaler ──────────────────────────────────


def test_tick_reports_zero_reaped_by_default():
    """No reap_dead_workers_fn provided → snap.reaped is 0, the default."""
    fake = _Fake(depth=0, current=0)
    a = _autoscaler(fake)  # no reap closure
    snap = a.tick()
    assert snap.reaped == 0


def test_tick_calls_reap_when_provided_and_reports_count():
    fake = _Fake(depth=0, current=0, reap_return=0)
    a = _autoscaler(fake, with_reap=True)
    snap = a.tick()
    assert fake.reap_calls == 1
    assert snap.reaped == 0


def test_tick_reports_reaped_count_when_nonzero():
    fake = _Fake(depth=0, current=0, reap_return=4)
    a = _autoscaler(fake, with_reap=True)
    snap = a.tick()
    assert snap.reaped == 4


def test_tick_reaping_does_not_perturb_scaling_decision():
    """Scaling decision is independent of reap output. Depth=2, current=0,
    max=2 → start 2 workers and also report whatever reap returns."""
    fake = _Fake(depth=2, current=0, reap_return=7)
    a = _autoscaler(fake, max_count=2, with_reap=True)
    snap = a.tick()
    assert snap.desired == 2
    assert snap.started == 2
    assert fake.starts == 2
    assert snap.reaped == 7


def _run_one_tick(a: Autoscaler) -> None:
    """Run exactly one iteration of ``a.run()`` then stop.

    The trick: queue_depth_fn is called first inside tick(); wrap it
    so it triggers a.stop() after returning. The loop then runs one
    iteration, logs the tick line, and exits on the next predicate
    check.
    """
    original = a.queue_depth_fn

    def stop_after() -> int:
        value = original()
        a.stop()
        return value

    a.queue_depth_fn = stop_after
    a.run()


def test_run_log_includes_reaped_column(caplog):
    """The per-tick log line carries the new reaped=N column."""
    fake = _Fake(depth=0, current=0, reap_return=2)
    a = _autoscaler(fake, with_reap=True)
    with caplog.at_level(logging.INFO, logger="treadmill.autoscaler"):
        _run_one_tick(a)
    tick_lines = [r.getMessage() for r in caplog.records if "tick:" in r.getMessage()]
    assert tick_lines, "expected at least one tick log line"
    assert "reaped=2" in tick_lines[-1]


def test_run_log_reaped_zero_renders_in_tick_line(caplog):
    fake = _Fake(depth=0, current=0, reap_return=0)
    a = _autoscaler(fake, with_reap=True)
    with caplog.at_level(logging.INFO, logger="treadmill.autoscaler"):
        _run_one_tick(a)
    tick_lines = [r.getMessage() for r in caplog.records if "tick:" in r.getMessage()]
    assert tick_lines
    assert "reaped=0" in tick_lines[-1]


# ── _container_age_seconds parser ─────────────────────────────────────────────


def test_container_age_seconds_handles_iso_with_z():
    finished = "2026-05-19T12:00:00.000000Z"
    now = datetime(2026, 5, 19, 12, 0, 45, tzinfo=timezone.utc).timestamp()
    age = _container_age_seconds(finished, now)
    assert age is not None
    assert 44.5 <= age <= 45.5


def test_container_age_seconds_handles_nanoseconds():
    """Docker emits 9-digit fractional seconds; Python only supports 6."""
    finished = "2026-05-19T12:00:00.123456789Z"
    now = datetime(2026, 5, 19, 12, 0, 30, tzinfo=timezone.utc).timestamp()
    age = _container_age_seconds(finished, now)
    assert age is not None
    assert 29 <= age <= 30


def test_container_age_seconds_returns_none_for_sentinel():
    assert _container_age_seconds("0001-01-01T00:00:00Z", 0.0) is None


def test_container_age_seconds_returns_none_for_missing():
    assert _container_age_seconds(None, 0.0) is None
    assert _container_age_seconds("", 0.0) is None


def test_container_age_seconds_returns_none_for_garbage():
    assert _container_age_seconds("not-a-timestamp", 0.0) is None


# ── reap closure against a fake docker client ─────────────────────────────────


def _iso(dt: datetime) -> str:
    """Render a datetime in the Z-suffixed form Docker uses."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fake_container(name: str, finished_at: str) -> MagicMock:
    c = MagicMock()
    c.name = name
    c.attrs = {"State": {"FinishedAt": finished_at}}
    return c


def test_reap_closure_removes_only_old_exited_containers(monkeypatch):
    """Build the reap closure the way main() does, point it at a fake
    docker client, and assert .remove() fires only on containers whose
    FinishedAt is older than _REAP_AGE_SECONDS."""
    import docker as _docker_pkg  # for the APIError class

    from treadmill_local import autoscaler as autoscaler_mod

    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
    # Two old (60s, 120s) and one fresh (5s ago, below the 30s threshold).
    old_a = _fake_container("worker-old-a", _iso(now - timedelta(seconds=60)))
    old_b = _fake_container("worker-old-b", _iso(now - timedelta(seconds=120)))
    fresh = _fake_container("worker-fresh", _iso(now - timedelta(seconds=5)))

    fake_client = MagicMock()
    fake_client.containers.list.return_value = [old_a, old_b, fresh]

    # Freeze time.time() so the closure compares against `now`.
    monkeypatch.setattr(autoscaler_mod.time, "time", lambda: now.timestamp())

    family = "demo"
    worker_labels = [
        "treadmill.managed=true",
        "treadmill.role=worker",
        f"treadmill.family={family}",
    ]

    def reap_dead_workers() -> int:
        ts = autoscaler_mod.time.time()
        exited = fake_client.containers.list(
            filters={"label": worker_labels, "status": "exited"}
        )
        reaped = 0
        for container in exited:
            finished_at = container.attrs.get("State", {}).get("FinishedAt")
            age = autoscaler_mod._container_age_seconds(finished_at, ts)
            if age is None or age < _REAP_AGE_SECONDS:
                continue
            try:
                container.remove()
                reaped += 1
            except _docker_pkg.errors.APIError:
                pass
        return reaped

    n = reap_dead_workers()
    assert n == 2
    old_a.remove.assert_called_once_with()
    old_b.remove.assert_called_once_with()
    fresh.remove.assert_not_called()

    # Confirm the label filter the closure sends to docker — this is the
    # invariant that prevents mass-pruning unrelated containers.
    fake_client.containers.list.assert_called_once_with(
        filters={
            "label": [
                "treadmill.managed=true",
                "treadmill.role=worker",
                f"treadmill.family={family}",
            ],
            "status": "exited",
        }
    )


def test_reap_closure_swallows_remove_failures(monkeypatch):
    """A single failed remove() must not break the loop or undercount the
    successful reaps."""
    import docker as _docker_pkg

    from treadmill_local import autoscaler as autoscaler_mod

    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
    good = _fake_container("worker-good", _iso(now - timedelta(seconds=60)))
    bad = _fake_container("worker-bad", _iso(now - timedelta(seconds=60)))
    bad.remove.side_effect = _docker_pkg.errors.APIError("boom")

    fake_client = MagicMock()
    fake_client.containers.list.return_value = [good, bad]
    monkeypatch.setattr(autoscaler_mod.time, "time", lambda: now.timestamp())

    def reap_dead_workers() -> int:
        ts = autoscaler_mod.time.time()
        exited = fake_client.containers.list(
            filters={"label": [], "status": "exited"}
        )
        reaped = 0
        for container in exited:
            age = autoscaler_mod._container_age_seconds(
                container.attrs.get("State", {}).get("FinishedAt"), ts
            )
            if age is None or age < _REAP_AGE_SECONDS:
                continue
            try:
                container.remove()
                reaped += 1
            except _docker_pkg.errors.APIError:
                pass
        return reaped

    assert reap_dead_workers() == 1
    good.remove.assert_called_once_with()
    bad.remove.assert_called_once_with()
