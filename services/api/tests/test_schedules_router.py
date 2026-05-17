"""Unit tests for the schedules router (ADR-0035).

Calls async route handlers directly with stub sessions — no Postgres required.
Integration (round-trip) tests live in ``test_integration_routers.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException

from treadmill_api.routers import schedules as schedules_router
from treadmill_api.routers.schedules import _expand_field, _next_fire


# ── _expand_field ─────────────────────────────────────────────────────────────


def test_expand_field_star():
    assert _expand_field("*", 0, 5) == {0, 1, 2, 3, 4, 5}


def test_expand_field_literal():
    assert _expand_field("3", 0, 59) == {3}


def test_expand_field_range():
    assert _expand_field("1-3", 0, 59) == {1, 2, 3}


def test_expand_field_step_from_star():
    assert _expand_field("*/15", 0, 59) == {0, 15, 30, 45}


def test_expand_field_range_with_step():
    assert _expand_field("0-30/10", 0, 59) == {0, 10, 20, 30}


def test_expand_field_list():
    assert _expand_field("1,3,5", 0, 59) == {1, 3, 5}


def test_expand_field_clamps_to_bounds():
    assert _expand_field("0-100", 0, 59) == set(range(60))


# ── _next_fire ────────────────────────────────────────────────────────────────


def test_next_fire_every_minute():
    after = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    nxt = _next_fire("* * * * *", after)
    assert nxt == datetime(2026, 5, 17, 12, 1, 0, tzinfo=timezone.utc)


def test_next_fire_specific_minute():
    after = datetime(2026, 5, 17, 9, 0, 0, tzinfo=timezone.utc)
    nxt = _next_fire("30 9 * * *", after)
    assert nxt == datetime(2026, 5, 17, 9, 30, 0, tzinfo=timezone.utc)


def test_next_fire_wraps_to_next_day():
    after = datetime(2026, 5, 17, 9, 45, 0, tzinfo=timezone.utc)
    nxt = _next_fire("0 9 * * *", after)
    assert nxt == datetime(2026, 5, 18, 9, 0, 0, tzinfo=timezone.utc)


def test_next_fire_day_of_week():
    # 2026-05-17 is a Sunday (weekday=6); next Monday is 2026-05-18
    after = datetime(2026, 5, 17, 0, 0, 0, tzinfo=timezone.utc)
    nxt = _next_fire("0 9 * * 1", after)  # Monday 09:00
    assert nxt == datetime(2026, 5, 18, 9, 0, 0, tzinfo=timezone.utc)


def test_next_fire_invalid_expr_returns_none():
    after = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert _next_fire("not-a-cron", after) is None


def test_next_fire_wrong_field_count():
    after = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert _next_fire("0 9 *", after) is None


def test_next_fire_impossible_expr_returns_none():
    # Feb 31 never exists
    after = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _next_fire("0 0 31 2 *", after) is None


def test_next_fire_dom_dow_or_semantics():
    # DOM=15, DOW=Monday — OR semantics: whichever day matches first.
    # Use after=2026-05-11 09:01 (Monday, just past 09:00) so the Monday
    # match is skipped; next candidates: DOM=15 on 2026-05-15 (Friday) and
    # DOW=Monday on 2026-05-18. May 15 comes first.
    after = datetime(2026, 5, 11, 9, 1, 0, tzinfo=timezone.utc)
    nxt = _next_fire("0 9 15 * 1", after)
    assert nxt is not None
    assert nxt == datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


# ── Stub session ──────────────────────────────────────────────────────────────


def _make_schedule(**kwargs: Any) -> Any:
    defaults = dict(
        id=uuid.uuid4(),
        cron_expression="0 9 * * *",
        workflow_id="wf-test",
        payload_template={},
        status="active",
        jitter_seconds=60,
        quiet_hours=None,
        quiet_tz="America/Los_Angeles",
        quiet_multiplier=6.0,
        quiet_max_seconds=43200,
        last_fired_at=None,
        created_by="tester",
        created_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    s = type("FakeSchedule", (), defaults)()
    return s


class _ScalarsResult:
    def __init__(self, items: list) -> None:
        self._items = items

    def all(self) -> list:
        return self._items


class _ExecuteResult:
    def __init__(self, items: list) -> None:
        self._items = items

    def scalars(self) -> _ScalarsResult:
        return _ScalarsResult(self._items)


class _StubSession:
    def __init__(self, schedules: list | None = None) -> None:
        self._schedules: dict[uuid.UUID, Any] = {
            s.id: s for s in (schedules or [])
        }
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.committed = False

    async def execute(self, *args: Any, **kwargs: Any) -> _ExecuteResult:
        return _ExecuteResult(list(self._schedules.values()))

    async def get(self, model: Any, pk: Any) -> Any | None:
        return self._schedules.get(pk)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if hasattr(obj, "id") and obj.id:
            self._schedules[obj.id] = obj

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)
        self._schedules.pop(obj.id, None)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, obj: Any) -> None:
        # Simulate server-side defaults that the DB would populate.
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "created_at") and obj.created_at is None:
            obj.created_at = datetime(2026, 5, 17, tzinfo=timezone.utc)


# ── list_schedules ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_schedules_empty() -> None:
    session = _StubSession()
    result = await schedules_router.list_schedules(session=session)  # type: ignore[arg-type]
    assert result == []


@pytest.mark.asyncio
async def test_list_schedules_returns_all() -> None:
    s1 = _make_schedule()
    s2 = _make_schedule(status="paused")
    session = _StubSession([s1, s2])
    result = await schedules_router.list_schedules(session=session)  # type: ignore[arg-type]
    assert len(result) == 2
    ids = {r.id for r in result}
    assert s1.id in ids
    assert s2.id in ids


@pytest.mark.asyncio
async def test_list_schedules_includes_next_fire_at() -> None:
    s = _make_schedule(cron_expression="0 9 * * *")
    session = _StubSession([s])
    result = await schedules_router.list_schedules(session=session)  # type: ignore[arg-type]
    assert len(result) == 1
    assert result[0].next_fire_at is not None


# ── create_schedule ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_schedule_adds_to_session() -> None:
    session = _StubSession()
    body = schedules_router.ScheduleCreateRequest(
        cron_expression="0 9 * * *",
        workflow_id="wf-crystallize",
        created_by="operator",
    )
    result = await schedules_router.create_schedule(body=body, session=session)  # type: ignore[arg-type]
    assert result.cron_expression == "0 9 * * *"
    assert result.workflow_id == "wf-crystallize"
    assert result.status == "active"
    assert session.committed


@pytest.mark.asyncio
async def test_create_schedule_with_payload_and_quiet_hours() -> None:
    session = _StubSession()
    body = schedules_router.ScheduleCreateRequest(
        cron_expression="*/5 * * * *",
        workflow_id="wf-check",
        jitter_seconds=30,
        quiet_hours="22-6",
        quiet_tz="UTC",
        payload_template={"env": "prod"},
        created_by="ci",
    )
    result = await schedules_router.create_schedule(body=body, session=session)  # type: ignore[arg-type]
    assert result.jitter_seconds == 30
    assert result.quiet_hours == "22-6"
    assert result.quiet_tz == "UTC"
    assert result.payload_template == {"env": "prod"}


# ── patch_schedule ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_schedule_pause() -> None:
    s = _make_schedule(status="active")
    session = _StubSession([s])
    body = schedules_router.SchedulePatchRequest(status="paused")
    result = await schedules_router.patch_schedule(
        schedule_id=s.id,
        body=body,
        session=session,  # type: ignore[arg-type]
    )
    assert result.status == "paused"
    assert session.committed


@pytest.mark.asyncio
async def test_patch_schedule_resume() -> None:
    s = _make_schedule(status="paused")
    session = _StubSession([s])
    body = schedules_router.SchedulePatchRequest(status="active")
    result = await schedules_router.patch_schedule(
        schedule_id=s.id,
        body=body,
        session=session,  # type: ignore[arg-type]
    )
    assert result.status == "active"


@pytest.mark.asyncio
async def test_patch_schedule_not_found() -> None:
    session = _StubSession()
    body = schedules_router.SchedulePatchRequest(status="paused")
    with pytest.raises(HTTPException) as exc_info:
        await schedules_router.patch_schedule(
            schedule_id=uuid.uuid4(),
            body=body,
            session=session,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 404


# ── delete_schedule ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_schedule_removes_from_session() -> None:
    s = _make_schedule()
    session = _StubSession([s])
    await schedules_router.delete_schedule(
        schedule_id=s.id,
        session=session,  # type: ignore[arg-type]
    )
    assert s in session.deleted
    assert session.committed


@pytest.mark.asyncio
async def test_delete_schedule_not_found() -> None:
    session = _StubSession()
    with pytest.raises(HTTPException) as exc_info:
        await schedules_router.delete_schedule(
            schedule_id=uuid.uuid4(),
            session=session,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 404
