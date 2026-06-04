"""SchedulerRunner — periodic cron-dispatch loop (ADR-0035).

Designed to run as a sibling asyncio task (task 4, scheduler-spawn-on-up,
wires it into the lifespan handler). Can also be driven directly from a
standalone ``asyncio.run()`` entry-point.

Main loop (every 30 s):
  1. SELECT active schedules from Postgres.
  2. For each, compute next_fire_time(cron, last_fired_at) + jitter.
  3. If effective fire time ≤ now AND not in a quiet window, fire:
       a. INSERT Event(entity_type="schedule", action="tick") row.
       b. UPDATE schedules.last_fired_at = now.
       c. Commit the session (persistence first; bus is notification-only).
       d. Publish ScheduledTick event to the event bus.
  4. Log publish failures; they do not roll back the DB write.

Startup (missed-tick replay, Q35.c):
  Iterate over the 4-hour look-back window. For each cron fire time between
  ``last_fired_at`` (or ``created_at``) and now, fire if jitter-adjusted
  time ≤ now and schedule is not quiet.

ADR-0069 self-heal: when spawned as a host subprocess by
``treadmill-local up``, ``main()`` constructs a ``StalenessGuard`` over
``treadmill_api`` and passes it to the runner. The loop calls
``maybe_reexec`` at the top of each iteration (before ``_tick``) so a
services/api/** PR merge propagates to the host-side scheduler within
one poll interval, rather than waiting for an operator to bounce
``treadmill-local up``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from treadmill_api.eventbus import EventPublisher, get_publisher
from treadmill_api.events.schedule import ScheduledTick
from treadmill_api.models.event import Event
from treadmill_api.models.schedule import Schedule
from treadmill_api.observability import get_tracer
from treadmill_api.scheduler.bounded_logging import RateLimitedErrorLogger
from treadmill_api.scheduler.cron import iter_fires, next_fire_time
from treadmill_api.scheduler.policy import calculate_jitter_seconds, is_quiet

logger = logging.getLogger("treadmill.scheduler")

POLL_INTERVAL_SECONDS = 30
MISSED_TICK_WINDOW_HOURS = 4


class SchedulerRunner:
    """Asyncio background task that polls for due schedules every 30 s.

    Lifecycle mirrors ``CoordinationConsumer``: construct with deps,
    ``await start()`` to launch the background task, ``await stop()`` to
    cancel it gracefully.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        publisher: EventPublisher | None = None,
        *,
        staleness_guard: Any = None,
        staleness_pid_file: Path | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._publisher = publisher
        # ADR-0069 self-heal: when set, ``_run`` consults the guard at
        # the top of each iteration (a safe re-exec point — no DB tx
        # in flight, no publish pending) and re-execs the process when
        # the watched package's bytes drifted from startup. ``None``
        # disables the check (in-process tests, the API-lifespan path
        # where the runner is a sibling asyncio task rather than a
        # standalone subprocess).
        self._staleness_guard = staleness_guard
        self._staleness_pid_file = staleness_pid_file
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Replay missed ticks then start the poll loop."""
        await self._replay_missed_ticks()
        self._task = asyncio.create_task(self._run(), name="scheduler-runner")
        logger.info("scheduler: runner started (poll interval=%ds)", POLL_INTERVAL_SECONDS)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("scheduler: runner stopped")

    # ── internal ──────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        # Rate-limit the loop's error path so a persistent failure (DB
        # unreachable, SNS credentials expired) doesn't dump a full
        # traceback every poll. First occurrence logs in full; repeats
        # are summarized; ``reset()`` after a successful tick re-arms
        # a fresh traceback for the next incident.
        error_logger = RateLimitedErrorLogger(logger)
        while True:
            # ADR-0069 safe-point: top of loop, before _tick(). Any DB
            # transaction from the previous tick has already committed
            # or rolled back; nothing is mid-flight. ``_maybe_reexec``
            # is a no-op when the guard is ``None`` (in-process tests
            # and the API-lifespan path); when set, it re-execs the
            # process via ``os.execv`` if the watched package bytes
            # have changed since startup.
            self._maybe_reexec()
            try:
                await self._tick()
                error_logger.reset()
            except Exception as exc:
                error_logger.log(exc, "scheduler: tick failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _maybe_reexec(self) -> None:
        """ADR-0069 staleness check. Synchronous: ``os.execv`` doesn't
        return on success, so dropping into asyncio is unnecessary.

        Errors in ``changed()`` are swallowed by the guard itself
        (mid-sync transient I/O shouldn't push us to re-exec into a
        broken state). A failed ``os.execv`` raises ``OSError`` and we
        let that propagate — the process is in an undefined state and
        crashing loudly beats running on stale code.
        """
        if self._staleness_guard is None:
            return
        if self._staleness_guard.changed():
            self._staleness_guard.reexec(self._staleness_pid_file)

    async def _tick(self) -> None:
        now = datetime.now(tz=timezone.utc)
        publisher = self._publisher or get_publisher()

        async with self._sessionmaker() as session:
            result = await session.execute(
                select(Schedule).where(Schedule.status == "active")
            )
            schedules = list(result.scalars().all())

        tracer = get_tracer("treadmill.scheduler")
        for schedule in schedules:
            with tracer.start_as_current_span(
                "scheduler.evaluate",
                attributes={"schedule.id": str(schedule.id)},
            ):
                try:
                    await self._maybe_fire(schedule, now, publisher)
                except Exception:
                    logger.exception(
                        "scheduler: error evaluating schedule %s", schedule.id
                    )

    async def _maybe_fire(
        self,
        schedule: Schedule,
        now: datetime,
        publisher: EventPublisher,
    ) -> None:
        ref = _ref_time(schedule)
        nft = next_fire_time(schedule.cron_expression, ref)
        jitter = calculate_jitter_seconds(str(schedule.id), schedule.jitter_seconds)
        effective = nft + timedelta(seconds=jitter)

        if effective > now:
            return

        if schedule.quiet_hours and is_quiet(now, schedule.quiet_hours, schedule.quiet_tz):
            return

        await self._fire(schedule, now, publisher)

    async def _fire(
        self,
        schedule: Schedule,
        fire_at: datetime,
        publisher: EventPublisher,
    ) -> None:
        """Persist the tick event + update last_fired_at, then publish."""
        event_id = uuid.uuid4()
        typed_payload = ScheduledTick(
            schedule_id=schedule.id,
            workflow_id=schedule.workflow_id,
            rendered_payload=schedule.payload_template,
        )
        encoded = typed_payload.model_dump(mode="json")

        async with self._sessionmaker.begin() as session:
            session.add(
                Event(
                    id=event_id,
                    entity_type=ScheduledTick.ENTITY_TYPE,
                    action=ScheduledTick.ACTION,
                    payload=encoded,
                )
            )
            await session.execute(
                update(Schedule)
                .where(Schedule.id == schedule.id)
                .values(last_fired_at=fire_at)
            )

        try:
            event_stub = _EventStub(event_id, ScheduledTick.ENTITY_TYPE, ScheduledTick.ACTION)
            await publisher.publish(event_stub, typed_payload)  # type: ignore[arg-type]
        except Exception:
            logger.warning(
                "scheduler: publish failed for schedule %s event %s (DB write committed)",
                schedule.id,
                event_id,
            )

        logger.info(
            "scheduler: fired schedule %s workflow=%s fire_at=%s",
            schedule.id,
            schedule.workflow_id,
            fire_at.isoformat(),
        )

    async def _replay_missed_ticks(self) -> None:
        """Fire any ticks missed within the 4-hour look-back window."""
        now = datetime.now(tz=timezone.utc)
        window_start = now - timedelta(hours=MISSED_TICK_WINDOW_HOURS)
        publisher = self._publisher or get_publisher()

        logger.info(
            "scheduler: replaying missed ticks in window [%s, %s)",
            window_start.isoformat(),
            now.isoformat(),
        )

        async with self._sessionmaker() as session:
            result = await session.execute(
                select(Schedule).where(Schedule.status == "active")
            )
            schedules = list(result.scalars().all())

        for schedule in schedules:
            try:
                await self._replay_schedule(schedule, window_start, now, publisher)
            except Exception:
                logger.exception(
                    "scheduler: replay failed for schedule %s", schedule.id
                )

    async def _replay_schedule(
        self,
        schedule: Schedule,
        window_start: datetime,
        now: datetime,
        publisher: EventPublisher,
    ) -> None:
        ref = _ref_time(schedule)
        search_start = max(ref, window_start)
        jitter = calculate_jitter_seconds(str(schedule.id), schedule.jitter_seconds)

        for fire_time in iter_fires(schedule.cron_expression, search_start, now):
            effective = fire_time + timedelta(seconds=jitter)
            if effective > now:
                continue
            if schedule.quiet_hours and is_quiet(
                effective, schedule.quiet_hours, schedule.quiet_tz
            ):
                continue
            logger.info(
                "scheduler: replaying missed tick for schedule %s at %s",
                schedule.id,
                fire_time.isoformat(),
            )
            await self._fire(schedule, effective, publisher)


# ── helpers ───────────────────────────────────────────────────────────────────


def _ref_time(schedule: Schedule) -> datetime:
    """Return the last-fired or created timestamp, always UTC-aware."""
    ref = schedule.last_fired_at or schedule.created_at
    if ref is None:
        return datetime.now(tz=timezone.utc)
    if ref.tzinfo is None:
        return ref.replace(tzinfo=timezone.utc)
    return ref


class _EventStub:
    """Minimal duck-typed stand-in for the Event ORM row used by the publisher.

    The publisher only reads ``id``, ``entity_type``, ``action``, and the
    optional FK IDs (all None for scheduled ticks). Using the stub avoids
    re-querying the DB after commit just to obtain the ORM instance.
    """

    def __init__(self, event_id: uuid.UUID, entity_type: str, action: str) -> None:
        self.id = event_id
        self.entity_type = entity_type
        self.action = action
        self.task_id = None
        self.plan_id = None
        self.run_id = None
        self.step_id = None


# ── Subprocess entrypoint ─────────────────────────────────────────────────────


async def _amain() -> None:
    """Async entry point: wire DB + publisher then drive SchedulerRunner.

    Reads from env:
      DATABASE_URL        — asyncpg-form Postgres URL (required)
      EVENTS_TOPIC_ARN    — SNS topic; falls back to LoggingEventPublisher
      AWS_DEFAULT_REGION  — boto3 region
      AWS_ENDPOINT_URL    — moto override; absent in dev-local/fully-remote
    """
    import signal

    import boto3
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from treadmill_api.eventbus import SNSEventPublisher, set_publisher

    db_url = os.environ["DATABASE_URL"]
    topic_arn = os.environ.get("EVENTS_TOPIC_ARN")
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    engine = create_async_engine(db_url, future=True, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    publisher: EventPublisher | None = None
    if topic_arn:
        sns_client = boto3.client("sns", region_name=region)
        publisher = SNSEventPublisher(sns_client, topic_arn)
        set_publisher(publisher)

    # ADR-0069: build the staleness guard from the dev-local adapter
    # package iff it's importable in this environment. The scheduler
    # subprocess is spawned by ``treadmill-local up`` (workspace install
    # → both packages present), and the import is deferred to runtime
    # so production deploys without the adapter on PYTHONPATH still
    # boot — they just won't self-heal, which is fine because production
    # uses container immutability for the same property.
    staleness_guard, staleness_pid_file = _build_staleness_wiring()

    runner = SchedulerRunner(
        sessionmaker=session_factory,
        publisher=publisher,
        staleness_guard=staleness_guard,
        staleness_pid_file=staleness_pid_file,
    )
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await runner.start()
    logger.info("scheduler: subprocess running; waiting for stop signal")
    await stop.wait()
    logger.info("scheduler: stop signal received; shutting down")
    await runner.stop()
    await engine.dispose()


def _build_staleness_wiring() -> tuple[Any, Path | None]:
    """Return ``(guard, pid_file)`` for the dev-local subprocess path.

    Defers the import of ``treadmill_local.staleness`` to runtime so
    production deploys without the adapter package on ``PYTHONPATH``
    still boot (the scheduler subprocess is dev-local-only by design;
    production uses container immutability for the same self-heal
    property). Returns ``(None, None)`` if the adapter isn't importable.

    Fingerprints ``treadmill_api`` (the scheduler's own package) so a
    ``services/api/**`` merge — picked up on disk by the deploy
    watcher's ``_sync_local_to_origin`` — triggers a re-exec within one
    poll interval. Pinning the ``module`` to this entrypoint means the
    re-exec'd process re-enters ``main()`` cleanly.
    """
    try:
        from treadmill_local.staleness import StalenessGuard
    except ImportError:
        return None, None
    guard = StalenessGuard("treadmill_api", module="treadmill_api.scheduler.runner")
    pid_file = Path(".treadmill-local") / "scheduler.pid"
    return guard, pid_file


def main() -> int:
    from treadmill_api.scheduler.bounded_logging import configure_rotating_logging

    # The subprocess owns its own log file — the parent passes the path
    # via env. Fall back to a sensible default if unset so a bare
    # ``python -m treadmill_api.scheduler.runner`` still has somewhere
    # to write.
    log_file_env = os.environ.get("TREADMILL_SCHEDULER_LOG_FILE")
    log_file = Path(log_file_env) if log_file_env else Path(".treadmill-local") / "scheduler.log"
    configure_rotating_logging(log_file)

    # ADR-0069: rewrite the PID file with our own pid at startup. The
    # parent's ``_start_scheduler_dev_local`` wrote ``proc.pid`` after
    # spawn, which matches us on first boot — but after an ``os.execv``
    # re-exec the new process owns the same pid file and must claim it
    # so the parent's ``_pid_alive`` + ``_stop_scheduler`` keep working.
    pid_file = Path(".treadmill-local") / "scheduler.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    asyncio.run(_amain())
    return 0


if __name__ == "__main__":
    sys.exit(main())
