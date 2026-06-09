"""Unit tests for ``coordination.auto_merge_loop.AutoMergeLoop``.

Pins the behavioral invariants extracted from ``CoordinationConsumer``:
- short-circuits when ``redis_client`` or ``github_client`` is unwired
- swallows non-cancellation exceptions to stay alive across transients
- ``CancelledError`` propagates so ``stop()`` cleans up
- ``start()`` is idempotent against a second call
- ``stop()`` is safe pre-start
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination.auto_merge_loop import AutoMergeLoop


pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_sessionmaker() -> MagicMock:
    return MagicMock(name="sessionmaker")


async def _let_one_tick_run(tick_seconds: float = 0.01) -> None:
    """Yield long enough for ``asyncio.sleep(tick_seconds)`` to elapse plus
    one more event-loop turn so the body after the sleep runs."""
    await asyncio.sleep(tick_seconds * 3)


async def test_start_and_stop_clean(fake_sessionmaker: MagicMock) -> None:
    """Start launches the task; stop cancels and clears it."""
    loop = AutoMergeLoop(
        sessionmaker=fake_sessionmaker, tick_seconds=0.01,
    )
    await loop.start()
    assert loop.is_running()
    await loop.stop()
    assert not loop.is_running()
    assert loop._task is None


async def test_start_is_idempotent(fake_sessionmaker: MagicMock) -> None:
    """Calling start twice does not create a second task."""
    loop = AutoMergeLoop(
        sessionmaker=fake_sessionmaker, tick_seconds=0.01,
    )
    await loop.start()
    first = loop._task
    await loop.start()
    assert loop._task is first
    await loop.stop()


async def test_stop_safe_when_never_started(
    fake_sessionmaker: MagicMock,
) -> None:
    """Stopping a loop that never started is a no-op, not an error."""
    loop = AutoMergeLoop(sessionmaker=fake_sessionmaker)
    await loop.stop()
    assert loop._task is None


async def test_short_circuit_when_redis_unwired(
    fake_sessionmaker: MagicMock,
) -> None:
    """``redis_client=None`` means the tick body skips
    ``fire_elapsed_auto_merges`` entirely."""
    fire = AsyncMock(return_value=0)
    with patch(
        "treadmill_api.coordination.triggers.fire_elapsed_auto_merges", fire,
    ):
        loop = AutoMergeLoop(
            sessionmaker=fake_sessionmaker,
            redis_client=None,
            github_client=MagicMock(),
            tick_seconds=0.01,
        )
        await loop.start()
        await _let_one_tick_run(0.01)
        await loop.stop()
    fire.assert_not_called()


async def test_short_circuit_when_github_unwired(
    fake_sessionmaker: MagicMock,
) -> None:
    """``github_client=None`` also skips the trigger."""
    fire = AsyncMock(return_value=0)
    with patch(
        "treadmill_api.coordination.triggers.fire_elapsed_auto_merges", fire,
    ):
        loop = AutoMergeLoop(
            sessionmaker=fake_sessionmaker,
            redis_client=MagicMock(),
            github_client=None,
            tick_seconds=0.01,
        )
        await loop.start()
        await _let_one_tick_run(0.01)
        await loop.stop()
    fire.assert_not_called()


async def test_fires_trigger_with_full_wiring(
    fake_sessionmaker: MagicMock,
) -> None:
    """When fully wired, each tick calls ``fire_elapsed_auto_merges`` with
    the constructor-supplied clients + sessionmaker."""
    fire = AsyncMock(return_value=0)
    redis_client = MagicMock(name="redis")
    github_client = MagicMock(name="github")
    with patch(
        "treadmill_api.coordination.triggers.fire_elapsed_auto_merges", fire,
    ):
        loop = AutoMergeLoop(
            sessionmaker=fake_sessionmaker,
            redis_client=redis_client,
            github_client=github_client,
            tick_seconds=0.01,
        )
        await loop.start()
        await _let_one_tick_run(0.01)
        await loop.stop()
    assert fire.called
    kwargs = fire.call_args.kwargs
    assert kwargs["redis_client"] is redis_client
    assert kwargs["github_client"] is github_client
    assert kwargs["sessionmaker"] is fake_sessionmaker


async def test_swallows_non_cancellation_exceptions(
    fake_sessionmaker: MagicMock,
) -> None:
    """A transient GitHub or Redis error during a tick is logged and the
    loop continues to the next tick — does not raise out."""
    fire = AsyncMock(side_effect=RuntimeError("transient github 500"))
    with patch(
        "treadmill_api.coordination.triggers.fire_elapsed_auto_merges", fire,
    ):
        loop = AutoMergeLoop(
            sessionmaker=fake_sessionmaker,
            redis_client=MagicMock(),
            github_client=MagicMock(),
            tick_seconds=0.01,
        )
        await loop.start()
        # Let several ticks happen — none should kill the loop.
        await _let_one_tick_run(0.01)
        assert loop.is_running()
        await loop.stop()


async def test_cancellation_propagates_through_stop(
    fake_sessionmaker: MagicMock,
) -> None:
    """``stop()`` cancels the underlying task; ``CancelledError`` is
    expected and suppressed inside ``stop()`` so the caller doesn't see
    it. Verified by reaching the assertion after stop returns."""
    loop = AutoMergeLoop(
        sessionmaker=fake_sessionmaker,
        redis_client=MagicMock(),
        github_client=MagicMock(),
        tick_seconds=10.0,  # long tick so sleep is what gets cancelled
    )
    await loop.start()
    # No sleep here — stop should cancel immediately.
    await loop.stop()
    assert not loop.is_running()


async def test_is_running_false_before_start(
    fake_sessionmaker: MagicMock,
) -> None:
    """A fresh instance reports not-running."""
    loop = AutoMergeLoop(sessionmaker=fake_sessionmaker)
    assert not loop.is_running()


async def test_loop_fired_count_logged_when_nonzero(
    fake_sessionmaker: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When a tick fires merges, the loop logs ``fired %d merge(s)``."""
    fire = AsyncMock(return_value=3)
    with patch(
        "treadmill_api.coordination.triggers.fire_elapsed_auto_merges", fire,
    ):
        loop = AutoMergeLoop(
            sessionmaker=fake_sessionmaker,
            redis_client=MagicMock(),
            github_client=MagicMock(),
            tick_seconds=0.01,
        )
        with caplog.at_level("INFO", logger="treadmill_api.coordination.auto_merge_loop"):
            await loop.start()
            await _let_one_tick_run(0.01)
            await loop.stop()
    assert any("fired 3 merge(s)" in r.message for r in caplog.records)
