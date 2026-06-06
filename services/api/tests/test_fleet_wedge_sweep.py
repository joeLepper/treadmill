"""Unit tests for the fleet-wedge sweep v1 (ADR-0075 §3 zero-workers).

Three behaviors:

  * A wedged family (worker_count=0 with recent spawn intent and a
    stale-enough heartbeat) is detected and one ``system.fleet_wedged``
    event is emitted with sub_signal='zero-workers'.
  * A healthy family (workers > 0, OR no recent spawn intent, OR
    heartbeat is fresh) is excluded by the SQL — sweep emits nothing.
  * Dedup: a second sweep tick reads the existing event row and no-ops.

Pure unit tests with mocked session/dispatcher — no DB, no autoscaler.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.fleet_wedge_sweep import (
    FLEET_WEDGE_SIGNAL,
    FLEET_WEDGE_SWEEP_WORKFLOW_ID,
    FLEET_WEDGE_ZERO_WORKERS_THRESHOLD,
    run_fleet_wedge_sweep,
)


class _Row:
    """A canned row matching the sweep's ``SELECT system_status`` shape."""

    def __init__(
        self,
        family: str,
        worker_count: int,
        last_spawn_at: datetime | None,
        last_spawn_error: str | None,
        consecutive_spawn_failures: int,
        updated_at: datetime,
    ) -> None:
        self.family = family
        self.worker_count = worker_count
        self.last_spawn_at = last_spawn_at
        self.last_spawn_error = last_spawn_error
        self.consecutive_spawn_failures = consecutive_spawn_failures
        self.updated_at = updated_at


class _IterableResult:
    """Mimics SQLAlchemy ``Result`` shape the sweep iterates."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)


def _dedup_probe(existing: bool) -> MagicMock:
    """The dedup ``SELECT events`` returns a result whose ``.first()``
    is ``None`` (proceed with emit) or a row (skip)."""
    r = MagicMock()
    r.first.return_value = MagicMock() if existing else None
    return r


# ── wedged family path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wedged_family_emits_one_event() -> None:
    """SQL returns one wedged row → one system.fleet_wedged event with
    sub_signal='zero-workers' and a payload describing the wedge."""
    now = datetime.now(timezone.utc)
    last_spawn = now - timedelta(minutes=10)
    heartbeat = now - FLEET_WEDGE_ZERO_WORKERS_THRESHOLD - timedelta(minutes=1)
    row = _Row(
        family="treadmill-agent",
        worker_count=0,
        last_spawn_at=last_spawn,
        last_spawn_error="docker build failed for treadmill-dashboard:dev",
        consecutive_spawn_failures=350,
        updated_at=heartbeat,
    )

    session = AsyncMock()
    # First execute: the sweep's SELECT system_status.
    # Second execute: the per-family dedup check (no existing).
    session.execute = AsyncMock(side_effect=[
        _IterableResult([row]),
        _dedup_probe(existing=False),
    ])

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    emitted = await run_fleet_wedge_sweep(session, dispatcher, now=now)

    assert emitted == 1
    dispatcher.persist_and_publish.assert_awaited_once()
    kwargs = dispatcher.persist_and_publish.await_args.kwargs
    assert kwargs["entity_type"] == "system"
    assert kwargs["action"] == "fleet_wedged"
    payload = kwargs["payload"]
    assert payload["family"] == "treadmill-agent"
    assert payload["sub_signal"] == "zero-workers"
    assert payload["worker_count"] == 0
    assert payload["consecutive_spawn_failures"] == 350
    assert payload["seconds_wedged"] > 0
    assert payload["signal"] == FLEET_WEDGE_SIGNAL
    assert payload["remediation_path"].endswith("image-build-stuck.md")
    assert payload["last_spawn_error"].startswith("docker build failed")


# ── healthy family path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthy_family_no_event() -> None:
    """SQL returns no rows (workers > 0, OR fresh heartbeat, OR no recent
    spawn intent) → no event emitted, no further session.execute calls."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterableResult([]))

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    emitted = await run_fleet_wedge_sweep(session, dispatcher)

    assert emitted == 0
    dispatcher.persist_and_publish.assert_not_awaited()
    assert session.execute.await_count == 1


# ── dedup: existing event for this family already in window ───────────────


@pytest.mark.asyncio
async def test_dedup_existing_event_skips_emit() -> None:
    """An existing system.fleet_wedged event for the same family with the
    same sub_signal in the activity window → dedup probe returns a row →
    sweep skips the emit."""
    now = datetime.now(timezone.utc)
    last_spawn = now - timedelta(minutes=10)
    heartbeat = now - FLEET_WEDGE_ZERO_WORKERS_THRESHOLD - timedelta(minutes=1)
    row = _Row(
        family="treadmill-agent",
        worker_count=0,
        last_spawn_at=last_spawn,
        last_spawn_error=None,
        consecutive_spawn_failures=0,
        updated_at=heartbeat,
    )

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult([row]),
        _dedup_probe(existing=True),
    ])

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    emitted = await run_fleet_wedge_sweep(session, dispatcher, now=now)

    assert emitted == 0
    dispatcher.persist_and_publish.assert_not_awaited()


# ── multiple families wedged simultaneously ──────────────────────────────


@pytest.mark.asyncio
async def test_multiple_wedged_families_emit_separately() -> None:
    """Two distinct wedged families → two events, both with sub_signal
    'zero-workers' but distinct family names. The dedup probe is called
    once per family."""
    now = datetime.now(timezone.utc)
    last_spawn = now - timedelta(minutes=10)
    heartbeat = now - FLEET_WEDGE_ZERO_WORKERS_THRESHOLD - timedelta(minutes=1)
    rows = [
        _Row("treadmill-agent", 0, last_spawn, None, 0, heartbeat),
        _Row("treadmill-build", 0, last_spawn, None, 0, heartbeat),
    ]

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _IterableResult(rows),
        _dedup_probe(existing=False),
        _dedup_probe(existing=False),
    ])

    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock()

    emitted = await run_fleet_wedge_sweep(session, dispatcher, now=now)

    assert emitted == 2
    assert dispatcher.persist_and_publish.await_count == 2
    families_emitted = {
        c.kwargs["payload"]["family"]
        for c in dispatcher.persist_and_publish.await_args_list
    }
    assert families_emitted == {"treadmill-agent", "treadmill-build"}


# ── routing seam: workflow_id slug surfaces correctly ────────────────────


def test_workflow_id_constant_pinned() -> None:
    """The schedule's workflow_id slug is the public contract that
    ``handle_scheduled_tick`` matches against. Pin it so a future rename
    fails this test rather than silently breaking the routing."""
    assert FLEET_WEDGE_SWEEP_WORKFLOW_ID == "wf-fleet-wedge-sweep"


# ── absent dispatcher (test stub) returns zero ────────────────────────────


@pytest.mark.asyncio
async def test_absent_dispatcher_returns_zero() -> None:
    """Test stubs may pass dispatcher=None; the sweep no-ops without
    raising, matching the existing convention in _emit_operator_escalation."""
    session = AsyncMock()
    emitted = await run_fleet_wedge_sweep(session, dispatcher=None)
    assert emitted == 0
    session.execute.assert_not_awaited()
