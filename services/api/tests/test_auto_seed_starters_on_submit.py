"""Unit tests for ``_auto_seed_starters_on_submit`` (post-mortem
surprise A of the combined ADR-0085+0086 plan).

The helper runs the empty-workflows auto-seed branch inside
``POST /api/v1/plans``. We isolate the four observable behaviors:

  * Happy path (``wf-author`` already present) — no seed call, no
    event, no log; fast-path overhead is the EXISTS check only.
  * Empty workflows table — sync seed runs via ``asyncio.to_thread``,
    one ``system.auto_seeded_starters`` event emitted with
    ``roles_seeded > 0`` and ``triggered_by="plan_submit"``.
  * Concurrent replica race — our EXISTS check sees an empty table
    but another replica seeded between our check and the
    ``SELECT FOR UPDATE`` inside the seed transaction. The helper
    returns 0 from the sync call → no event emitted (the other
    replica's commit already fired one).
  * Seed-transaction failure — ``HTTPException(500)`` is raised with
    the auto-seed branch named in the ``detail`` so the operator
    sees the cause, not a silent persist.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from treadmill_api.routers.plans import _auto_seed_starters_on_submit


def _make_session(wf_author_present: bool) -> MagicMock:
    """Build an AsyncSession-shaped stub whose ``scalar`` returns
    whatever value the EXISTS check expects.

    The helper only calls ``session.scalar`` (for the EXISTS check)
    and hands the session to ``dispatcher.persist_and_publish``; the
    dispatcher itself is mocked so the session never actually writes.
    """
    session = MagicMock()
    session.scalar = AsyncMock(return_value=bool(wf_author_present))
    return session


def _make_dispatcher() -> MagicMock:
    """Build a Dispatcher-shaped stub with the methods the helper uses."""
    dispatcher = MagicMock()
    dispatcher.persist_and_publish = AsyncMock(return_value=None)
    return dispatcher


def _make_settings() -> MagicMock:
    """The helper only forwards ``settings`` into the sync seed wrapper
    (which the tests patch); no fields are read from settings here."""
    return MagicMock(name="Settings")


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_skips_seed_call_and_event() -> None:
    """When ``wf-author`` is registered (the steady-state case), the
    helper returns immediately after the EXISTS check — no seed call,
    no event. This is the cost the happy path pays per plan submit."""
    session = _make_session(wf_author_present=True)
    dispatcher = _make_dispatcher()
    settings = _make_settings()

    with patch(
        "treadmill_api.starters.run_auto_seed_starters_sync"
    ) as seed_mock:
        await _auto_seed_starters_on_submit(session, settings, dispatcher)

    session.scalar.assert_awaited_once()
    seed_mock.assert_not_called()
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_workflows_triggers_seed_and_emits_event() -> None:
    """When the EXISTS check finds no ``wf-author``, the sync seed runs
    via ``asyncio.to_thread`` and one ``system.auto_seeded_starters``
    event is emitted with the seeded-count + ``triggered_by`` pin."""
    session = _make_session(wf_author_present=False)
    dispatcher = _make_dispatcher()
    settings = _make_settings()

    # The brief's "~8 on a fresh DB" pattern — the value flows directly
    # into the event payload's ``roles_seeded`` field.
    with patch(
        "treadmill_api.starters.run_auto_seed_starters_sync",
        return_value=8,
    ) as seed_mock:
        await _auto_seed_starters_on_submit(session, settings, dispatcher)

    seed_mock.assert_called_once_with(settings)
    dispatcher.persist_and_publish.assert_awaited_once()
    call = dispatcher.persist_and_publish.await_args
    assert call.kwargs["entity_type"] == "system"
    assert call.kwargs["action"] == "auto_seeded_starters"
    payload = call.kwargs["payload"]
    assert payload.roles_seeded == 8
    assert payload.triggered_by == "plan_submit"


@pytest.mark.asyncio
async def test_concurrent_replica_lost_race_emits_no_event() -> None:
    """Our EXISTS check saw an empty table, but another replica
    seeded between then and our ``SELECT FOR UPDATE``. The sync seed
    returns 0 (skipped the seed branch internally). We must NOT emit
    our own event — the winning replica's commit already fired one.
    Pinning this prevents double-counted audit events on the rare
    concurrent-fresh-DB-first-submit branch."""
    session = _make_session(wf_author_present=False)
    dispatcher = _make_dispatcher()
    settings = _make_settings()

    with patch(
        "treadmill_api.starters.run_auto_seed_starters_sync",
        return_value=0,
    ) as seed_mock:
        await _auto_seed_starters_on_submit(session, settings, dispatcher)

    seed_mock.assert_called_once_with(settings)
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_failure_raises_500_with_named_cause() -> None:
    """When the sync seed raises, the helper raises HTTPException(500)
    whose ``detail`` names auto-seed as the cause and includes the
    original exception text. The plan is NOT persisted — we never
    silently degrade to "persist the plan anyway" against an unseeded
    DB. This is exactly the silent-failure mode the post-mortem
    flagged."""
    session = _make_session(wf_author_present=False)
    dispatcher = _make_dispatcher()
    settings = _make_settings()

    with patch(
        "treadmill_api.starters.run_auto_seed_starters_sync",
        side_effect=RuntimeError("DB permission denied"),
    ) as seed_mock:
        with pytest.raises(HTTPException) as exc_info:
            await _auto_seed_starters_on_submit(
                session, settings, dispatcher
            )

    seed_mock.assert_called_once_with(settings)
    assert exc_info.value.status_code == 500
    detail = exc_info.value.detail
    assert "auto-seed of starter roles failed" in detail
    assert "DB permission denied" in detail
    dispatcher.persist_and_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_exists_check_uses_wf_author_id() -> None:
    """The EXISTS check key is ``wf-author`` specifically, not "any
    workflow row." That keeps the check structural — a future cleanup
    that wipes incidental workflows still triggers a re-seed only if
    the load-bearing starter is the one missing. Pinning the value
    here so the EXISTS predicate stays the load-bearing intent."""
    session = _make_session(wf_author_present=True)
    dispatcher = _make_dispatcher()

    await _auto_seed_starters_on_submit(
        session, _make_settings(), dispatcher
    )

    # The scalar call took an EXISTS-shaped statement; compile the
    # bound parameters to verify the value is ``wf-author``.
    call_args = session.scalar.await_args.args
    stmt = call_args[0]
    compiled = stmt.compile(compile_kwargs={"literal_binds": False})
    assert "wf-author" in compiled.params.values()
