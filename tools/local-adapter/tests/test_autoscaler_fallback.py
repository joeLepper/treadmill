"""Tests for autoscaler image-build fallback + escalation behavior."""

from __future__ import annotations

from typing import Any

from treadmill_local.autoscaler import Autoscaler


class _Fake:
    """Test double for autoscaler dependencies."""

    def __init__(
        self,
        visible: int = 0,
        in_flight: int = 0,
        current: int = 0,
    ):
        self.visible = visible
        self.in_flight = in_flight
        self.current = current
        self.starts = 0
        self.starts_no_build = 0
        self.reap_calls = 0
        self.heartbeats: list[dict[str, Any]] = []
        self.build_error_count = 0

    def queue_depth(self) -> tuple[int, int]:
        return self.visible, self.in_flight

    def worker_count(self) -> int:
        return self.current

    def start_worker(self) -> None:
        self.starts += 1
        if self.build_error_count > 0:
            self.build_error_count -= 1
            raise RuntimeError(
                "docker build failed for treadmill-api:dev; refusing to start "
                "containers with a stale image."
            )
        self.current += 1

    def start_worker_no_build(self) -> None:
        self.starts_no_build += 1
        self.current += 1

    def reap_dead_workers(self) -> int:
        self.reap_calls += 1
        return 0

    def heartbeat(self, payload: dict[str, Any]) -> None:
        self.heartbeats.append(payload)


def _autoscaler(
    fake: _Fake,
    *,
    min_count: int = 0,
    max_count: int = 1,
) -> Autoscaler:
    return Autoscaler(
        queue_depth_fn=fake.queue_depth,
        worker_count_fn=fake.worker_count,
        start_worker_fn=fake.start_worker,
        start_worker_no_build_fn=fake.start_worker_no_build,
        reap_dead_workers_fn=fake.reap_dead_workers,
        min_count=min_count,
        max_count=max_count,
        tick_seconds=0.0,
        heartbeat_fn=fake.heartbeat,
    )


def test_env_var_disables_build() -> None:
    """TREADMILL_AUTOSCALER_BUILD_IMAGES=false disables image builds."""
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {"TREADMILL_AUTOSCALER_BUILD_IMAGES": "false"}):
        build_images = (
            os.environ.get("TREADMILL_AUTOSCALER_BUILD_IMAGES", "true").lower()
            not in ("false", "0", "no")
        )
        assert build_images is False

    with patch.dict(os.environ, {"TREADMILL_AUTOSCALER_BUILD_IMAGES": "true"}):
        build_images = (
            os.environ.get("TREADMILL_AUTOSCALER_BUILD_IMAGES", "true").lower()
            not in ("false", "0", "no")
        )
        assert build_images is True


def test_k_consecutive_failures_triggers_fallback() -> None:
    """After K=12 consecutive build failures, fallback is triggered."""
    fake = _Fake(visible=1, current=0)
    fake.build_error_count = 12
    a = _autoscaler(fake, max_count=1)

    for i in range(12):
        try:
            a.tick()
        except RuntimeError:
            pass

    assert a._fallback_ticks == 1, f"Expected fallback_ticks=1, got {a._fallback_ticks}"
    a.tick()
    assert fake.starts_no_build == 1


def test_f_fallback_ticks_marks_image_build_broken() -> None:
    """After F=3 fallback ticks, emit image_build_broken via heartbeat."""
    fake = _Fake(visible=1, current=0)
    fake.build_error_count = 100
    a = _autoscaler(fake, max_count=1)

    for i in range(12):
        try:
            a.tick()
        except RuntimeError:
            pass

    for i in range(3):
        a.tick()

    assert any(
        hb.get("image_build_broken") for hb in fake.heartbeats
    ), f"Expected image_build_broken in heartbeats, got: {fake.heartbeats}"


def test_successful_build_resets_counter() -> None:
    """A successful build resets the counter to 0."""
    fake = _Fake(visible=1, current=0)
    fake.build_error_count = 2
    a = _autoscaler(fake, max_count=1)

    for i in range(2):
        a.tick()

    a.tick()
    assert a._consecutive_build_failures == 0
    assert a._fallback_ticks == 0
