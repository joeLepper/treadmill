"""Unit tests for ``treadmill_api.seed.system_plan`` (ADR-0057).

Validates the SYSTEM_PLAN_ID constant + the ``seed_system_plan_if_empty``
direct DB path: it inserts the system Plan row + the ``plan.activated``
event when absent, and no-ops when the row already exists.

Integration tests (live Postgres) live separately; here we drive a stub
sync session that records ``add`` / ``execute`` / ``commit`` calls.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

from treadmill_api.seed.system_plan import (
    SYSTEM_PLAN_ID,
    seed_system_plan_if_empty,
)


# ── Stub session ──────────────────────────────────────────────────────────────


class _ScalarOneOrNoneResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSession:
    """Sync session double — execute returns ``existing`` (Plan row or None);
    ``add`` records inserted entities; ``commit`` is recorded."""

    def __init__(self, *, existing: Any | None) -> None:
        self._existing = existing
        self.added: list[Any] = []
        self.committed = False

    def execute(self, *args: Any, **kwargs: Any) -> _ScalarOneOrNoneResult:
        return _ScalarOneOrNoneResult(self._existing)

    def add(self, entity: Any) -> None:
        self.added.append(entity)

    def commit(self) -> None:
        self.committed = True


# ── Constant invariants ──────────────────────────────────────────────────────


def test_system_plan_id_is_canonical_sentinel() -> None:
    """The system Plan id is a stable UUID literal — every component that
    references the system Plan reads from this constant rather than
    looking it up."""
    assert SYSTEM_PLAN_ID == uuid.UUID("00000000-0000-0000-0000-000000000001")


# ── seed_system_plan_if_empty: fresh DB ──────────────────────────────────────


def test_seed_system_plan_if_empty_fresh_db_inserts_plan_and_activated_event() -> None:
    """On a fresh DB (no existing system Plan), the seeder inserts both a
    Plan row AND a ``plan.activated`` event in the same transaction. The
    activation event is what lifts the system Plan into
    ``derived_status='active'`` via the plan_status VIEW so synthetic-
    task dispatch doesn't park in deferred-dispatch."""
    session = _StubSession(existing=None)

    seeded = seed_system_plan_if_empty(session)

    assert seeded == 1
    assert session.committed is True
    assert len(session.added) == 2, (
        f"expected 2 inserts (Plan + plan.activated event), got "
        f"{len(session.added)}: {session.added}"
    )

    # Identify the Plan row vs. the Event row by attribute presence
    # (intent is a Plan field; entity_type is an Event field). This is
    # robust to insertion order.
    plans = [a for a in session.added if hasattr(a, "intent")]
    events = [a for a in session.added if hasattr(a, "entity_type")]
    assert len(plans) == 1
    assert len(events) == 1

    plan = plans[0]
    assert plan.id == SYSTEM_PLAN_ID
    assert plan.repo, "system Plan repo must be non-empty (Plan.repo is NOT NULL)"
    assert plan.created_by == "auto-seed"

    event = events[0]
    assert event.entity_type == "plan"
    assert event.action == "activated"
    assert event.plan_id == SYSTEM_PLAN_ID


# ── seed_system_plan_if_empty: idempotency ───────────────────────────────────


def test_seed_system_plan_if_empty_is_idempotent_when_row_exists() -> None:
    """Multi-replica startup safety: when the system Plan already exists,
    the seeder returns 0 without inserting anything or committing. The
    other replica got there first."""
    existing_plan = MagicMock()
    existing_plan.id = SYSTEM_PLAN_ID
    session = _StubSession(existing=existing_plan)

    seeded = seed_system_plan_if_empty(session)

    assert seeded == 0
    assert session.added == []
    assert session.committed is False
