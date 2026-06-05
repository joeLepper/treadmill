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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("treadmill.autoscaler")


@dataclass
class AutoscalerTick:
    """Snapshot of one control-loop iteration. Useful for observability and tests.

    ``visible`` + ``in_flight`` together describe the *total* workload the
    autoscaler is sizing against. Sizing against visible-only under-
    provisions when long-running messages are in flight (see ADR-0018 +
    2026-05-19 observation: a 16-minute author step blocked the queue
    even though max=8).
    """

    visible: int
    in_flight: int
    current: int
    desired: int
    started: int
    reaped: int = 0

    @property
    def total(self) -> int:
        """Total workload = visible + in_flight. The autoscaler sizes on this."""
        return self.visible + self.in_flight


# Worker containers exit fast (ADR-0018: one-shot per message). Without
# reaping they pile up — 102 exited workers observed in dev-local on
# 2026-05-19. The autoscaler reaps any container matching the worker
# label set whose FinishedAt is older than this threshold. The grace
# window lets an operator inspect logs from a just-exited worker before
# the container disappears.
_REAP_AGE_SECONDS = 30


def _container_age_seconds(finished_at: str | None, now: float) -> float | None:
    """Return age in seconds from a Docker ``FinishedAt`` ISO timestamp.

    Returns ``None`` when the timestamp is missing or the
    ``0001-01-01T00:00:00Z`` sentinel Docker returns for containers
    that haven't finished yet (which shouldn't appear under a
    ``status=exited`` filter, but be defensive).
    """
    if not finished_at:
        return None
    if finished_at.startswith("0001-01-01"):
        return None
    # Docker emits e.g. "2026-05-19T12:34:56.123456789Z". Python's
    # fromisoformat doesn't accept the trailing Z and chokes on
    # nanosecond precision pre-3.11. Normalize both.
    s = finished_at
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, _, tail = s.partition(".")
        # tail looks like "123456789+00:00" — split fractional from tz.
        frac = ""
        tz = ""
        for i, ch in enumerate(tail):
            if ch in "+-":
                frac = tail[:i]
                tz = tail[i:]
                break
        else:
            frac = tail
        # Truncate fractional seconds to microseconds (6 digits).
        frac = frac[:6]
        s = f"{head}.{frac}{tz}" if frac else f"{head}{tz}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now - dt.timestamp()


class Autoscaler:
    """A target-tracking-style autoscaler driven by SQS workload.

    Workload = ``ApproximateNumberOfMessages`` (visible) +
    ``ApproximateNumberOfMessagesNotVisible`` (in-flight). Visible-only
    sizing under-provisions: with one long-running message in flight and
    one queued, visible=1 yields desired=1 even though the queued message
    has no worker to pick it up until the in-flight message completes.
    Summing the two restores the autoscaler's whole point — match worker
    count to *total* work, not just unclaimed work.

    Policy: ``desired = clamp(total, min, max)``. With ``max=1`` this scales
    one worker at a time; with higher max it scales out to match demand up
    to the cap. Scale-down is not emulated — workers exit after each step
    and the loop simply stops launching replacements when desired drops.
    """

    def __init__(
        self,
        *,
        queue_depth_fn: Callable[[], tuple[int, int]],
        worker_count_fn: Callable[[], int],
        start_worker_fn: Callable[[], None],
        min_count: int,
        max_count: int,
        tick_seconds: float = 2.0,
        reap_dead_workers_fn: Callable[[], int] | None = None,
        staleness_guard: Any = None,
        staleness_pid_file: Path | None = None,
        heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
        start_worker_no_build_fn: Callable[[], None] | None = None,
    ) -> None:
        if min_count < 0:
            raise ValueError(f"min_count must be >= 0, got {min_count}")
        if max_count < min_count:
            raise ValueError(f"max_count ({max_count}) must be >= min_count ({min_count})")
        # queue_depth_fn returns (visible, in_flight). The autoscaler sums
        # them into the workload signal used for sizing — see class docstring.
        self.queue_depth_fn = queue_depth_fn
        self.worker_count_fn = worker_count_fn
        self.start_worker_fn = start_worker_fn
        self.reap_dead_workers_fn = reap_dead_workers_fn or (lambda: 0)
        self.min_count = min_count
        self.max_count = max_count
        self.tick_seconds = tick_seconds
        # ADR-0069 self-heal: the production wiring constructs a
        # ``StalenessGuard`` over the ``treadmill_local`` package and
        # passes it here; ``run()`` consults it at the TOP of each
        # iteration (before ``tick()``) and re-execs the process when
        # the bytes drifted. ``None`` disables the check (legacy
        # fully-local path + tests that don't care about self-heal).
        self.staleness_guard = staleness_guard
        self.staleness_pid_file = staleness_pid_file
        self.heartbeat_fn = heartbeat_fn or (lambda _: None)
        self.start_worker_no_build_fn = start_worker_no_build_fn or (lambda: None)
        self._stop_event = threading.Event()
        # Image build fallback tracking (K=12, F=3)
        self._consecutive_build_failures = 0
        self._fallback_ticks = 0
        self._image_build_broken_reported = False

    def tick(self) -> AutoscalerTick:
        """Run one iteration of the control loop.

        Order matters: scale first (worker_count_fn() reads running
        containers, which is unaffected by exited ones), then reap.
        Reap errors are swallowed by the closure itself so they never
        break the tick.

        Image build fallback: when _ensure_images_built fails repeatedly,
        after K=12 consecutive failures run ONE tick with build_images=False.
        After F=3 fallback ticks, emit image_build_broken via heartbeat.
        """
        visible, in_flight = self.queue_depth_fn()
        total = visible + in_flight
        current = self.worker_count_fn()
        desired = self._compute_desired(total)
        delta = desired - current
        started = max(0, delta)

        # Check if we're in fallback mode (previous tick(s) hit K threshold)
        in_fallback = self._fallback_ticks > 0

        for _ in range(started):
            try:
                if in_fallback:
                    # Fallback tick: call the no-build variant
                    self.start_worker_no_build_fn()
                else:
                    # Normal tick: try to build images
                    self.start_worker_fn()
                # Build succeeded; reset consecutive failure counter
                self._consecutive_build_failures = 0
            except RuntimeError as exc:
                # Only count docker build errors, not other RuntimeErrors
                if "docker build" in str(exc).lower():
                    if not in_fallback:
                        # Increment counter during normal (non-fallback) ticks
                        self._consecutive_build_failures += 1
                        if self._consecutive_build_failures >= 12:
                            # Hit K=12 threshold, trigger fallback for next iteration
                            logger.error(
                                "image build failed 12 times; "
                                "will use fallback (last-known-good image)"
                            )
                            self._consecutive_build_failures = 0
                            self._fallback_ticks = 1
                    # Don't re-raise; error is logged by run()'s exception handler
                else:
                    # Not a build error, re-raise for normal error handling
                    raise

        # Update fallback state tracking
        if in_fallback:
            # We completed a fallback tick; increment counter and check escalation
            self._fallback_ticks += 1
            # After F=3 fallback ticks, emit image_build_broken escalation
            if self._fallback_ticks > 3 and not self._image_build_broken_reported:
                self._image_build_broken_reported = True
                logger.error("image_build_broken: escalating after 3+ fallback ticks")
                self.heartbeat_fn({"image_build_broken": True})

        reaped = self.reap_dead_workers_fn()
        return AutoscalerTick(
            visible=visible,
            in_flight=in_flight,
            current=current,
            desired=desired,
            started=started,
            reaped=reaped,
        )

    def _compute_desired(self, total: int) -> int:
        """Track total workload, clamped to [min_count, max_count]."""
        return max(self.min_count, min(total, self.max_count))

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
        from treadmill_local.staleness import maybe_reexec
        from treadmill_local.subprocess_logging import RateLimitedErrorLogger
        # Rate-limit the loop's error path so a persistent failure
        # (queue unreachable, expired credentials) doesn't dump a full
        # traceback every tick. First occurrence logs in full; repeats
        # are summarized; ``reset()`` after a successful tick re-arms
        # a fresh traceback for the next incident.
        error_logger = RateLimitedErrorLogger(logger)
        logger.info(
            "autoscaler starting (min=%d, max=%d, tick=%.1fs)",
            self.min_count, self.max_count, self.tick_seconds,
        )
        while not self._stop_event.is_set():
            # ADR-0069 safe-point: top of loop, before tick(), so a
            # re-exec never tears down a mid-flight container start
            # or reap. ``maybe_reexec`` is a no-op when the guard is
            # ``None`` (legacy path) or when source has not changed.
            maybe_reexec(self.staleness_guard, self.staleness_pid_file)
            try:
                t = self.tick()
                # The tick line stays at INFO unconditionally — it's the
                # liveness heartbeat that ``treadmill-local status``
                # reads via log mtime. The reaped column lets operators
                # see reap activity in the same line.
                logger.info(
                    "tick: visible=%d in_flight=%d total=%d current=%d "
                    "desired=%d started=%d reaped=%d",
                    t.visible, t.in_flight, t.total, t.current,
                    t.desired, t.started, t.reaped,
                )
                error_logger.reset()
            except Exception as exc:
                error_logger.log(exc, "tick failed; continuing")
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

    from treadmill_local.runtime import (
        AUTOSCALER_LOG_FILE,
        AUTOSCALER_PID_FILE,
        LABEL_KEY,
        STATE_DIR,
        LocalRuntime,
    )
    from treadmill_local.staleness import StalenessGuard
    from treadmill_local.subprocess_logging import configure_rotating_logging

    # The subprocess owns its own log file — the parent passes the
    # path via env. Fall back to the package default if unset so a
    # bare ``python -m treadmill_local.autoscaler`` still has somewhere
    # to write.
    log_file_env = os.environ.get("TREADMILL_AUTOSCALER_LOG_FILE")
    log_file = Path(log_file_env) if log_file_env else AUTOSCALER_LOG_FILE
    configure_rotating_logging(log_file)

    # ADR-0069: rewrite the PID file with our own pid at startup. The
    # parent's ``_start_autoscaler*`` wrote ``proc.pid`` after spawn,
    # which matches us on first boot — but after an ``os.execv`` re-exec
    # the new process owns the same pid file and must claim it so the
    # parent's ``_pid_alive`` + ``_stop_autoscaler`` keep working.
    STATE_DIR.mkdir(exist_ok=True)
    AUTOSCALER_PID_FILE.write_text(str(os.getpid()))

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

    from treadmill_local.docker_client import DockerClientAdapter

    # ADR-0060 Step 3b is feature-flagged ON by default as of ADR-0064
    # Step 3. The cross-network DNS gap that originally kept this flag
    # default-off (workers on the internal-only ``treadmill-egress``
    # bridge couldn't resolve ``treadmill-api`` on ``treadmill-local``
    # and exited 1 on the installation-token mint) is closed by the
    # ADR-0064 multi-attach: the API + egress proxy now sit on both
    # networks, and the ADR-0065 real-boot smoke gate has run green
    # against this default before the flip ships. With the flag on,
    # workers spawn on ``treadmill-egress`` (internal, no gateway) and
    # route external HTTP/HTTPS through ``treadmill-egress-proxy`` with
    # per-worker install credentials and an allowlist JSON. The flag
    # stays as an operator escape hatch: flip it back to ``false`` when
    # debugging the egress proxy itself (a suspected proxy bug, a
    # crash-looping proxy container blocking all workers, or an
    # allowlist misconfiguration that needs the unproxied path to
    # bisect against). Workers then spawn on the operator-default
    # network like they did pre-#92 — no proxy, no per-worker
    # isolation, but they run.
    egress_proxy_enabled = (
        os.environ.get("TREADMILL_EGRESS_PROXY_ENABLED", "true").lower()
        in ("true", "1", "yes")
    )

    adapter: DockerClientAdapter | None = None
    if egress_proxy_enabled:
        from treadmill_local.egress_proxy import (
            ensure_egress_network,
            ensure_egress_proxy_container,
        )

        adapter = DockerClientAdapter(docker_client)
        egress_config_dir = infra_dir / "egress-proxy-config"
        # mkdir BEFORE the proxy spawns. Docker auto-creates absent mount
        # paths as root (the daemon's UID); a root-owned dir blocks the
        # autoscaler (running as the operator's UID) from writing the
        # per-worker allowlist JSON later. Idempotent on existing
        # operator-owned dirs.
        egress_config_dir.mkdir(parents=True, exist_ok=True)
        ensure_egress_network(adapter)
        ensure_egress_proxy_container(adapter, egress_config_dir)

    # Read the build_images flag from env; default to True.
    build_images = (
        os.environ.get("TREADMILL_AUTOSCALER_BUILD_IMAGES", "true").lower()
        not in ("false", "0", "no")
    )

    if deployment_id is not None:
        # Dev-local: build LocalRuntime with the deployment_config so
        # ``start_worker_once`` calls into the dev-local credential
        # injection path (ADR-0019). The YAML is loaded fresh here (the
        # parent's in-memory state doesn't cross the subprocess boundary).
        from treadmill_local.deployment_config import load_deployment_yaml
        cfg = load_deployment_yaml(deployment_id)
        runtime = LocalRuntime(
            infra_dir=infra_dir, deployment_config=cfg, build_images=build_images
        )
    else:
        runtime = LocalRuntime(infra_dir=infra_dir, build_images=build_images)

    def get_depth() -> tuple[int, int]:
        """Return (visible, in_flight) message counts in one SQS call.

        The autoscaler sums these into the workload signal it sizes on.
        Visible-only under-provisioned by ignoring long-running in-flight
        messages — see Autoscaler class docstring + ADR-0018.
        """
        attrs = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        visible = int(attrs.get("ApproximateNumberOfMessages", "0"))
        in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", "0"))
        return visible, in_flight

    worker_labels = [
        f"{LABEL_KEY}=true",
        "treadmill.role=worker",
        f"treadmill.family={family}",
    ]

    def count_workers() -> int:
        return len(
            docker_client.containers.list(
                filters={
                    "label": worker_labels,
                    "status": "running",
                }
            )
        )

    def start_worker() -> None:
        runtime.start_worker_once(family, docker_adapter=adapter)

    def start_worker_no_build() -> None:
        # Temporarily disable image builds for fallback ticks
        old_build = runtime.build_images
        try:
            runtime.build_images = False
            runtime.start_worker_once(family, docker_adapter=adapter)
        finally:
            runtime.build_images = old_build

    def reap_dead_workers() -> int:
        """Remove exited worker containers older than ``_REAP_AGE_SECONDS``.

        Label filter is the same set ``count_workers`` uses
        (``treadmill.managed=true`` + ``treadmill.role=worker`` +
        ``treadmill.family=<family>``) so this can never touch a
        non-worker container. Each ``.remove()`` is guarded so one
        failed reap (already gone, racing operator, etc.) doesn't
        break the tick.
        """
        now = time.time()
        try:
            exited = docker_client.containers.list(
                filters={"label": worker_labels, "status": "exited"}
            )
        except docker.errors.APIError as exc:
            logger.warning("reap: list call failed: %s", exc)
            return 0
        reaped = 0
        for container in exited:
            finished_at = container.attrs.get("State", {}).get("FinishedAt")
            age = _container_age_seconds(finished_at, now)
            if age is None or age < _REAP_AGE_SECONDS:
                continue
            try:
                container.remove()
                reaped += 1
            except docker.errors.APIError as exc:
                logger.warning(
                    "reap: remove %s failed: %s", container.name, exc
                )
        return reaped

    # ADR-0069: fingerprint our package bytes at startup; the loop
    # consults this each iteration to decide whether to re-exec. The
    # entrypoint module is pinned so ``os.execv`` re-enters this exact
    # ``main()`` rather than the package root.
    staleness_guard = StalenessGuard(module="treadmill_local.autoscaler")

    autoscaler = Autoscaler(
        queue_depth_fn=get_depth,
        worker_count_fn=count_workers,
        start_worker_fn=start_worker,
        reap_dead_workers_fn=reap_dead_workers,
        min_count=min_count,
        max_count=max_count,
        tick_seconds=tick,
        staleness_guard=staleness_guard,
        staleness_pid_file=AUTOSCALER_PID_FILE,
        start_worker_no_build_fn=start_worker_no_build,
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
