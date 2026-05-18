"""Autoscaler — local equivalent of ECS Application Auto Scaling.

The autoscaler runs as a subprocess of `treadmill-local up`. It polls SQS
depth on a fixed interval, compares to running worker count, and reconciles
toward a desired count derived from the policy declared in CDK
(``AWS::ApplicationAutoScaling::ScalableTarget``).

Workers exit after each message (``EXIT_AFTER_STEP=true``); the autoscaler
launches replacements only when policy dictates. Mid-step termination is
not emulated locally — see ADR-0002.

The class is split from its subprocess entrypoint deliberately. ``Autoscaler``
takes injectable callables for queue-depth, worker-count, and start-worker
so it can be unit-tested without Docker or moto. The ``main()`` function is
the production wiring that constructs real callables against ``LocalRuntime``.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("treadmill.autoscaler")


@dataclass
class AutoscalerTick:
    """Snapshot of one control-loop iteration. Useful for observability and tests."""

    depth: int
    current: int
    desired: int
    started: int


class Autoscaler:
    """A target-tracking-style autoscaler driven by SQS queue depth.

    Policy: ``desired = clamp(depth, min, max)``. With ``max=1`` this scales
    one worker at a time; with higher max it scales out to match demand up
    to the cap. Scale-down is not emulated — workers exit after each step
    and the loop simply stops launching replacements when desired drops.
    """

    def __init__(
        self,
        *,
        queue_depth_fn: Callable[[], int],
        worker_count_fn: Callable[[], int],
        start_worker_fn: Callable[[], None],
        min_count: int,
        max_count: int,
        tick_seconds: float = 2.0,
    ) -> None:
        if min_count < 0:
            raise ValueError(f"min_count must be >= 0, got {min_count}")
        if max_count < min_count:
            raise ValueError(f"max_count ({max_count}) must be >= min_count ({min_count})")
        self.queue_depth_fn = queue_depth_fn
        self.worker_count_fn = worker_count_fn
        self.start_worker_fn = start_worker_fn
        self.min_count = min_count
        self.max_count = max_count
        self.tick_seconds = tick_seconds
        self._stop_event = threading.Event()

    def tick(self) -> AutoscalerTick:
        """Run one iteration of the control loop."""
        depth = self.queue_depth_fn()
        current = self.worker_count_fn()
        desired = self._compute_desired(depth)
        delta = desired - current
        started = max(0, delta)
        for _ in range(started):
            self.start_worker_fn()
        return AutoscalerTick(depth=depth, current=current, desired=desired, started=started)

    def _compute_desired(self, depth: int) -> int:
        """Track depth, clamped to [min_count, max_count]."""
        return max(self.min_count, min(depth, self.max_count))

    def run(self) -> None:
        """Loop until stop() is called or SIGTERM is received.

        Logs ``tick:`` at INFO on every iteration unconditionally. This
        is the autoscaler's heartbeat — observers read the log file's
        mtime as a liveness signal (``treadmill-local status``
        compares mtime to ``tick_seconds × 5``). The 2026-05-17 silent-
        death failure mode (process alive, loop ceased) becomes
        observable as soon as the log mtime falls outside the
        threshold.
        """
        logger.info(
            "autoscaler starting (min=%d, max=%d, tick=%.1fs)",
            self.min_count, self.max_count, self.tick_seconds,
        )
        while not self._stop_event.is_set():
            try:
                t = self.tick()
                logger.info(
                    "tick: depth=%d current=%d desired=%d started=%d",
                    t.depth, t.current, t.desired, t.started,
                )
            except Exception:
                logger.exception("tick failed; continuing")
            self._stop_event.wait(self.tick_seconds)
        logger.info("autoscaler stopped")

    def stop(self) -> None:
        self._stop_event.set()


# ── ScalableTarget parsing ────────────────────────────────────────────────────


def parse_scalable_target_bounds(properties: dict[str, Any]) -> tuple[int, int]:
    """Read MinCapacity / MaxCapacity from a ScalableTarget's Properties dict.

    Defaults to (0, 1) if either is missing. Raises if values aren't ints.
    """
    min_v = properties.get("MinCapacity", 0)
    max_v = properties.get("MaxCapacity", 1)
    if not isinstance(min_v, int) or not isinstance(max_v, int):
        raise TypeError(
            f"MinCapacity/MaxCapacity must be ints, got {type(min_v).__name__}/{type(max_v).__name__}"
        )
    return int(min_v), int(max_v)


# ── Subprocess entrypoint ─────────────────────────────────────────────────────


def main() -> int:
    """Production entrypoint: instantiate LocalRuntime, wire real callables, run.

    Branches on ``TREADMILL_AUTOSCALER_DEPLOYMENT_ID``:

      * **Set** — dev-local mode (ADR-0018). Loads the deployment YAML
        and constructs ``LocalRuntime(deployment_config=cfg)`` so each
        ``start_worker_once`` call triggers the host-side credential
        fetch + env-var injection per ADR-0019. AWS endpoints are real
        AWS (no moto override); credentials resolve via the inherited
        ``AWS_PROFILE`` from the parent's env.
      * **Unset** — fully-local mode (legacy). Constructs
        ``LocalRuntime(infra_dir=infra_dir)`` against the moto endpoint
        the parent injected as ``AWS_ENDPOINT_URL`` with the standard
        moto dummy credentials.
    """
    # Imports are local to keep `Autoscaler` itself dependency-free for tests.
    import boto3
    import docker

    from treadmill_local.runtime import LABEL_KEY, LocalRuntime

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    infra_dir = Path(os.environ["TREADMILL_INFRA_DIR"])
    family = os.environ["TREADMILL_AUTOSCALER_FAMILY"]
    queue_url = os.environ["TREADMILL_AUTOSCALER_QUEUE_URL"]
    min_count = int(os.environ.get("TREADMILL_AUTOSCALER_MIN", "0"))
    max_count = int(os.environ.get("TREADMILL_AUTOSCALER_MAX", "1"))
    tick = float(os.environ.get("TREADMILL_AUTOSCALER_TICK_SECONDS", "2"))
    deployment_id = os.environ.get("TREADMILL_AUTOSCALER_DEPLOYMENT_ID")

    # The subprocess inherits AWS endpoint config from the parent process.
    # Fully-local: ``AWS_ENDPOINT_URL`` points at the host-mapped moto port.
    # Dev-local: no endpoint override; boto3 talks to real AWS via
    # ``AWS_PROFILE`` + ``AWS_DEFAULT_REGION``.
    sqs = boto3.client(
        "sqs",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )
    docker_client = docker.from_env()

    if deployment_id is not None:
        # Dev-local: build LocalRuntime with the deployment_config so
        # ``start_worker_once`` calls into the dev-local credential
        # injection path (ADR-0019). The YAML is loaded fresh here (the
        # parent's in-memory state doesn't cross the subprocess boundary).
        from treadmill_local.deployment_config import load_deployment_yaml
        cfg = load_deployment_yaml(deployment_id)
        runtime = LocalRuntime(infra_dir=infra_dir, deployment_config=cfg)
    else:
        runtime = LocalRuntime(infra_dir=infra_dir)

    def get_depth() -> int:
        attrs = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        # Visible only — in-flight messages are already being processed.
        return int(attrs.get("ApproximateNumberOfMessages", "0"))

    def count_workers() -> int:
        return len(
            docker_client.containers.list(
                filters={
                    "label": [
                        f"{LABEL_KEY}=true",
                        "treadmill.role=worker",
                        f"treadmill.family={family}",
                    ],
                    "status": "running",
                }
            )
        )

    def start_worker() -> None:
        runtime.start_worker_once(family)

    autoscaler = Autoscaler(
        queue_depth_fn=get_depth,
        worker_count_fn=count_workers,
        start_worker_fn=start_worker,
        min_count=min_count,
        max_count=max_count,
        tick_seconds=tick,
    )

    def _on_signal(_signum: int, _frame: Any) -> None:
        logger.info("received signal; stopping")
        autoscaler.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    autoscaler.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
