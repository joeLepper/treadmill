"""Auto-merge polling loop (ADR-0084 Task 3C).

Extracted from ``CoordinationConsumer`` as a step toward ADR-0084 §11's
coordinator-led execution model. v1 keeps the loop in the API service
(this module is owned by ``CoordinationConsumer``'s lifecycle); a future
phase wires it to a coordinator Claude session instead.

The loop ticks every ``tick_seconds`` (default 5s) and calls
``triggers.fire_elapsed_auto_merges`` to detect Redis-tracked
``treadmill:auto-merge-deadline:*`` keys whose deadlines have elapsed,
re-verify mergeability, and issue ``PUT /repos/{repo}/pulls/{pr_number}/merge``.

Behavior preserved from the in-consumer implementation:
- Short-circuits cleanly when ``redis_client`` or ``github_client`` is
  unwired (the per-tick check rather than constructor-time bail keeps
  unit tests' partial wiring exercises working).
- All non-cancellation exceptions are swallowed at the loop level so a
  transient GitHub API or Redis error doesn't kill the polling task.
- ``CancelledError`` propagates so ``stop()`` can clean up.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

_DEFAULT_TICK_SECONDS = 5.0


class AutoMergeLoop:
    """Background polling task that fires Redis-tracked auto-merge deadlines.

    Lifecycle owned by ``CoordinationConsumer`` for v1. The consumer
    constructs an instance in ``__init__`` and calls ``start()`` /
    ``stop()`` from its own lifecycle methods.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis_client: Any | None = None,
        github_client: Any | None = None,
        tick_seconds: float = _DEFAULT_TICK_SECONDS,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.redis_client = redis_client
        self.github_client = github_client
        self.tick_seconds = tick_seconds
        self._stopped = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Launch the polling task. Idempotent: a second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="auto-merge-poll")
        logger.info("auto-merge loop started: tick_seconds=%s", self.tick_seconds)

    async def stop(self) -> None:
        """Signal stop + cancel the task + await its termination.

        Mirrors the inline ``CoordinationConsumer.stop`` shutdown pattern:
        flips the stopped flag, cancels, suppresses ``CancelledError`` on
        the await, logs any unexpected exception.
        """
        self._stopped = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("auto-merge loop raised on shutdown")
        self._task = None
        logger.info("auto-merge loop stopped")

    def is_running(self) -> bool:
        """Whether the polling task is currently alive (started + not done)."""
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        """Tick loop body. Sleeps, checks wiring, fires elapsed merges."""
        while not self._stopped:
            try:
                await asyncio.sleep(self.tick_seconds)
            except asyncio.CancelledError:
                raise
            if self.redis_client is None or self.github_client is None:
                continue
            try:
                # Local import avoids a circular dependency between
                # ``triggers`` (which may import from this module via
                # ``coordination/__init__``) and ``auto_merge_loop``.
                from treadmill_api.coordination.triggers import (
                    fire_elapsed_auto_merges,
                )
                fired = await fire_elapsed_auto_merges(
                    redis_client=self.redis_client,
                    sessionmaker=self.sessionmaker,
                    github_client=self.github_client,
                )
                if fired:
                    logger.info(
                        "auto-merge poll: fired %d merge(s) this tick", fired,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("auto-merge poll loop raised; continuing")
