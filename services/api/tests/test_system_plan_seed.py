"""Unit tests for ``treadmill_api.seed.system_plan``.

Validates the SYSTEM_PLAN_ID sentinel and the two seeding functions:

  * ``seed_system_plan_if_absent`` — DB-direct path (mock session).
  * ``seed_system_plan`` — HTTP-driven check path (mock API client).

Idempotency contract: running the seed twice against a DB that already has
the system Plan is a no-op (returns ``False``, no additional INSERTs).

Validation:
  ``cd services/api && uv run pytest tests/test_system_plan_seed.py -q``
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, call

import pytest

from treadmill_api.seed.system_plan import (
    SYSTEM_PLAN_ID,
    SYSTEM_PLAN_TITLE,
    seed_system_plan_if_absent,
)


# ── SYSTEM_PLAN_ID invariants ─────────────────────────────────────────────────


def test_system_plan_id_is_sentinel() -> None:
    assert SYSTEM_PLAN_ID == uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_system_plan_id_is_uuid() -> None:
    assert isinstance(SYSTEM_PLAN_ID, uuid.UUID)


def test_system_plan_title_value() -> None:
    assert SYSTEM_PLAN_TITLE == "system: scheduler"


# ── seed_system_plan_if_absent — DB path ──────────────────────────────────────


def _make_session(existing_plan: object = None) -> MagicMock:
    """Mock session where ``session.get(Plan, SYSTEM_PLAN_ID)`` returns
    ``existing_plan`` (None = not found; any other object = exists)."""
    session = MagicMock()
    session.get.return_value = existing_plan
    return session


def test_seed_if_absent_inserts_on_fresh_db() -> None:
    session = _make_session(None)
    result = seed_system_plan_if_absent(session, repo="testorg/repo")
    assert result is True


def test_seed_if_absent_adds_plan_and_event() -> None:
    """Two objects are session.add'd: the Plan row + the plan.activated event."""
    from treadmill_api.models.event import Event
    from treadmill_api.models.plan import Plan

    session = _make_session(None)
    seed_system_plan_if_absent(session, repo="testorg/repo")

    added = [c.args[0] for c in session.add.call_args_list]
    assert len(added) == 2
    assert isinstance(added[0], Plan)
    assert isinstance(added[1], Event)


def test_seed_if_absent_plan_fields() -> None:
    from treadmill_api.models.plan import Plan

    session = _make_session(None)
    seed_system_plan_if_absent(session, repo="testorg/repo")

    added_plan = next(
        c.args[0] for c in session.add.call_args_list
        if hasattr(c.args[0], "intent")
    )
    assert added_plan.id == SYSTEM_PLAN_ID
    assert added_plan.repo == "testorg/repo"
    assert added_plan.intent == SYSTEM_PLAN_TITLE
    assert added_plan.created_by == "scheduler"


def test_seed_if_absent_event_is_activated() -> None:
    from treadmill_api.models.event import Event

    session = _make_session(None)
    seed_system_plan_if_absent(session, repo="testorg/repo")

    added_event = next(
        c.args[0] for c in session.add.call_args_list
        if hasattr(c.args[0], "action")
    )
    assert isinstance(added_event, Event)
    assert added_event.entity_type == "plan"
    assert added_event.action == "activated"
    assert added_event.plan_id == SYSTEM_PLAN_ID


def test_seed_if_absent_skips_when_plan_exists() -> None:
    session = _make_session(MagicMock())  # truthy → plan already exists
    result = seed_system_plan_if_absent(session, repo="testorg/repo")
    assert result is False


def test_seed_if_absent_no_add_when_skipped() -> None:
    session = _make_session(MagicMock())
    seed_system_plan_if_absent(session, repo="testorg/repo")
    session.add.assert_not_called()


def test_seed_if_absent_no_commit_when_skipped() -> None:
    session = _make_session(MagicMock())
    seed_system_plan_if_absent(session, repo="testorg/repo")
    session.commit.assert_not_called()


def test_seed_if_absent_commits_on_insert() -> None:
    session = _make_session(None)
    seed_system_plan_if_absent(session, repo="testorg/repo")
    session.commit.assert_called_once()


def test_seed_if_absent_idempotent() -> None:
    """Two sequential calls: first inserts (True), second skips (False)."""
    # First call — plan absent
    session1 = _make_session(None)
    result1 = seed_system_plan_if_absent(session1, repo="testorg/repo")
    assert result1 is True

    # Second call — plan already present
    session2 = _make_session(MagicMock())
    result2 = seed_system_plan_if_absent(session2, repo="testorg/repo")
    assert result2 is False
    session2.add.assert_not_called()


def test_seed_if_absent_uses_provided_repo() -> None:
    from treadmill_api.models.plan import Plan

    session = _make_session(None)
    seed_system_plan_if_absent(session, repo="custom/repo")

    added_plan = next(
        c.args[0] for c in session.add.call_args_list
        if isinstance(c.args[0], Plan)
    )
    assert added_plan.repo == "custom/repo"


def test_seed_if_absent_checks_correct_plan_id() -> None:
    """session.get must be called with the sentinel UUID."""
    from treadmill_api.models.plan import Plan

    session = _make_session(None)
    seed_system_plan_if_absent(session, repo="testorg/repo")

    session.get.assert_called_once_with(Plan, SYSTEM_PLAN_ID)
