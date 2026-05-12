"""Unit tests for treadmill_api.dependencies — probes + aggregation."""

from __future__ import annotations

import asyncio

import pytest

from treadmill_api.dependencies import (
    CoordinationProbe,
    PostgresProbe,
    ProbeResult,
    ProbeStatus,
    RedisProbe,
    overall_status,
    run_probes,
)


# ── PostgresProbe ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_postgres_probe_returns_not_configured_when_engine_is_none():
    probe = PostgresProbe(engine=None)
    result = await probe.check()
    assert result.name == "postgres"
    assert result.status is ProbeStatus.NOT_CONFIGURED
    assert result.detail is None


@pytest.mark.asyncio
async def test_postgres_probe_returns_unreachable_on_engine_failure():
    """A real engine pointed at an unreachable host produces UNREACHABLE.

    We use a host:port that won't resolve so the connection fails fast."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        "postgresql+asyncpg://u:p@nonexistent.invalid:5432/db",
        connect_args={"timeout": 1.0},
    )
    probe = PostgresProbe(engine=engine)
    result = await probe.check()
    assert result.name == "postgres"
    assert result.status is ProbeStatus.UNREACHABLE
    assert result.detail is not None
    await engine.dispose()


# ── RedisProbe ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redis_probe_returns_not_configured_when_client_is_none():
    probe = RedisProbe(client=None)
    result = await probe.check()
    assert result.name == "redis"
    assert result.status is ProbeStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_redis_probe_returns_unreachable_when_client_raises():
    import redis.asyncio as redis_async

    # An invalid host makes ping() raise on connect.
    client = redis_async.Redis.from_url(
        "redis://nonexistent.invalid:6379/0",
        socket_connect_timeout=1.0,
    )
    probe = RedisProbe(client=client)
    result = await probe.check()
    assert result.name == "redis"
    assert result.status is ProbeStatus.UNREACHABLE
    assert result.detail is not None
    await client.aclose()


# ── Aggregation ───────────────────────────────────────────────────────────────


def test_overall_status_ok_when_all_ok():
    results = [
        ProbeResult("a", ProbeStatus.OK),
        ProbeResult("b", ProbeStatus.OK),
    ]
    assert overall_status(results) is ProbeStatus.OK


def test_overall_status_ok_when_some_not_configured():
    """not_configured does not flip overall status."""
    results = [
        ProbeResult("a", ProbeStatus.OK),
        ProbeResult("b", ProbeStatus.NOT_CONFIGURED),
    ]
    assert overall_status(results) is ProbeStatus.OK


def test_overall_status_unreachable_when_any_unreachable():
    results = [
        ProbeResult("a", ProbeStatus.OK),
        ProbeResult("b", ProbeStatus.UNREACHABLE, detail="boom"),
    ]
    assert overall_status(results) is ProbeStatus.UNREACHABLE


def test_overall_status_ok_with_empty_probe_list():
    assert overall_status([]) is ProbeStatus.OK


# ── run_probes ────────────────────────────────────────────────────────────────


class _StaticProbe:
    """Helper for testing run_probes — returns a pre-baked result."""

    def __init__(self, name: str, status: ProbeStatus):
        self.name = name
        self._status = status

    async def check(self) -> ProbeResult:
        return ProbeResult(self.name, self._status)


# ── CoordinationProbe ─────────────────────────────────────────────────────────


class _StubConsumer:
    """Stand-in for ``CoordinationConsumer`` exposing only ``is_running``.

    The probe deliberately reaches through the public method (per the
    2026-05-11 closure plan C.6) so we can drive it with a stub instead
    of wiring a full SQS + sessionmaker.
    """

    def __init__(self, running: bool) -> None:
        self._running = running

    def is_running(self) -> bool:
        return self._running


@pytest.mark.asyncio
async def test_coordination_probe_returns_not_configured_when_consumer_is_none():
    """No consumer constructed (env vars unset) → not_configured. Does not
    flip overall readiness; the API can boot without a consumer for
    healthcheck-only inspection."""
    probe = CoordinationProbe(consumer=None)
    result = await probe.check()
    assert result.name == "coordination_consumer"
    assert result.status is ProbeStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_coordination_probe_reports_running_when_task_alive():
    """A real consumer with a live poll task → ok. We use a fresh
    ``asyncio.Task`` that sleeps for a second so the task is alive but
    not yet finished when the probe runs."""

    class _LiveConsumer:
        def __init__(self) -> None:
            self._task = asyncio.create_task(asyncio.sleep(1))

        def is_running(self) -> bool:
            return self._task is not None and not self._task.done()

    consumer = _LiveConsumer()
    try:
        probe = CoordinationProbe(consumer=consumer)
        result = await probe.check()
        assert result.name == "coordination_consumer"
        assert result.status is ProbeStatus.OK
    finally:
        consumer._task.cancel()
        try:
            await consumer._task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_coordination_probe_reports_stopped_when_task_done():
    """A consumer whose task has finished (returned or raised) → unreachable.
    The probe surfaces this as a 503 on ``/health/ready``."""

    class _DeadConsumer:
        def __init__(self) -> None:
            async def _noop() -> None:
                return None
            self._task = asyncio.create_task(_noop())

        def is_running(self) -> bool:
            return self._task is not None and not self._task.done()

    consumer = _DeadConsumer()
    # Let the task complete.
    await consumer._task
    probe = CoordinationProbe(consumer=consumer)
    result = await probe.check()
    assert result.name == "coordination_consumer"
    assert result.status is ProbeStatus.UNREACHABLE
    assert result.detail is not None


@pytest.mark.asyncio
async def test_coordination_probe_reports_stopped_when_task_none():
    """A consumer that was constructed but never started (no ``_task``)
    counts as not-running → unreachable."""
    probe = CoordinationProbe(consumer=_StubConsumer(running=False))
    result = await probe.check()
    assert result.status is ProbeStatus.UNREACHABLE


@pytest.mark.asyncio
async def test_run_probes_runs_each_in_order():
    probes = [
        _StaticProbe("first", ProbeStatus.OK),
        _StaticProbe("second", ProbeStatus.UNREACHABLE),
        _StaticProbe("third", ProbeStatus.NOT_CONFIGURED),
    ]
    results = await run_probes(probes)
    assert [r.name for r in results] == ["first", "second", "third"]
    assert results[0].status is ProbeStatus.OK
    assert results[1].status is ProbeStatus.UNREACHABLE
    assert results[2].status is ProbeStatus.NOT_CONFIGURED
