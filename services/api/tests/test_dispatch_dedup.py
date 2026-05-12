"""Unit tests for the dispatch dedup builders + helper (ADR-0026).

Covers:

  * Each builder produces the expected dedup key for a synthetic event.
  * Workflows that opt out (wf-author, wf-plan, unknown) return None.
  * Events missing required fields produce None (graceful opt-out
    until the normalizer is extended).
  * Edge cases: special characters in repo names, large pr_numbers,
    empty strings.
  * ``maybe_dispatch_with_dedup``:
      - None key → unconditional dispatch (returns the run id).
      - First call with a key → dispatch + insert dedup row.
      - Second call with the same key → ``IntegrityError`` → skipped.
      - dispatch_fn not called when IntegrityError fires.

The integration test (test_integration_dispatch_dedup.py) covers the
full flow against live Postgres.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from treadmill_api.coordination.dispatch_dedup import (
    DEDUP_KEY_BUILDERS,
    build_dedup_key,
    maybe_dispatch_with_dedup,
)


# ── Builders: happy path ─────────────────────────────────────────────────────


def test_wf_review_builder_emits_pr_and_head_sha() -> None:
    """``wf-review:<repo>:pr=<N>,sha=<head_sha>`` — the review
    workflow dedupes on (PR, HEAD SHA): new SHA = new diff = new
    review; same SHA = identical content = no new review."""
    key = build_dedup_key(
        "wf-review",
        {
            "repo": "joeLepper/treadmill",
            "pr_number": 10,
            "head_sha": "b89360c1aaaabbbbccccddddeeeeffff00001111",
        },
    )
    assert key == (
        "wf-review:joeLepper/treadmill:pr=10,"
        "sha=b89360c1aaaabbbbccccddddeeeeffff00001111"
    )


def test_wf_feedback_builder_emits_review_id() -> None:
    """``wf-feedback:<repo>:review=<review_id>`` — one feedback per
    review. ``review_id`` is the GitHub review's node id."""
    key = build_dedup_key(
        "wf-feedback",
        {
            "repo": "joeLepper/treadmill",
            "review_id": "PRR_kwDOSb12345",
        },
    )
    assert key == "wf-feedback:joeLepper/treadmill:review=PRR_kwDOSb12345"


def test_wf_ci_fix_builder_emits_check_run_id() -> None:
    """``wf-ci-fix:<repo>:check_run=<check_run_id>`` — one fix per
    failing check_run."""
    key = build_dedup_key(
        "wf-ci-fix",
        {
            "repo": "joeLepper/treadmill",
            "check_run_id": 12345,
        },
    )
    assert key == "wf-ci-fix:joeLepper/treadmill:check_run=12345"


def test_wf_conflict_builder_emits_pr_and_base_sha() -> None:
    """``wf-conflict:<repo>:pr=<N>,sha=<base_sha>`` — same base = same
    conflict = same resolution."""
    key = build_dedup_key(
        "wf-conflict",
        {
            "repo": "joeLepper/treadmill",
            "pr_number": 10,
            "base_sha": "cafebabe" * 5,
        },
    )
    assert key == (
        "wf-conflict:joeLepper/treadmill:pr=10,"
        "sha=cafebabecafebabecafebabecafebabecafebabe"
    )


# ── Builders: opt-outs ───────────────────────────────────────────────────────


def test_wf_author_opts_out_of_dedup() -> None:
    """wf-author runs are dispatched per Task; task-level dedup is the
    existing ``tasks`` PK. No builder registered → returns None."""
    assert build_dedup_key("wf-author", {"repo": "x/y", "pr_number": 1}) is None


def test_wf_plan_opts_out_of_dedup() -> None:
    """wf-plan dispatches from ``plan_doc_merged`` events; the
    ADR-0021 handler already dedupes by uuid5(repo:path@sha). No
    builder registered → returns None."""
    assert build_dedup_key("wf-plan", {"repo": "x/y"}) is None


def test_unknown_workflow_opts_out_of_dedup() -> None:
    """Workflows not in DEDUP_KEY_BUILDERS implicitly opt out."""
    assert build_dedup_key("wf-bogus", {"repo": "x/y"}) is None


# ── Builders: missing fields (graceful opt-out at v0) ────────────────────────


def test_wf_review_returns_none_when_head_sha_missing() -> None:
    """The normalizer's ``pr_review_submitted`` payload does NOT emit
    ``head_sha`` today. The builder gracefully opts out → unconditional
    dispatch until the normalizer is extended."""
    assert build_dedup_key(
        "wf-review", {"repo": "x/y", "pr_number": 5},
    ) is None


def test_wf_review_returns_none_when_pr_number_missing() -> None:
    assert build_dedup_key(
        "wf-review", {"repo": "x/y", "head_sha": "abc"},
    ) is None


def test_wf_review_returns_none_when_repo_missing() -> None:
    assert build_dedup_key(
        "wf-review", {"pr_number": 5, "head_sha": "abc"},
    ) is None


def test_wf_feedback_returns_none_when_review_id_missing() -> None:
    """The normalizer does NOT emit ``review_id`` today; until it
    does, this builder is a graceful no-op."""
    assert build_dedup_key("wf-feedback", {"repo": "x/y"}) is None


def test_wf_ci_fix_returns_none_when_check_run_id_missing() -> None:
    """The normalizer does NOT emit ``check_run_id`` today."""
    assert build_dedup_key("wf-ci-fix", {"repo": "x/y"}) is None


def test_wf_conflict_returns_none_when_base_sha_missing() -> None:
    """The conflict-sweep emits ``head_sha`` (the conflicting PR's
    head), not ``base_sha``. Until the sweep is extended, this
    builder gracefully opts out."""
    assert build_dedup_key(
        "wf-conflict", {"repo": "x/y", "pr_number": 5},
    ) is None


# ── Builders: edge cases ─────────────────────────────────────────────────────


def test_wf_review_handles_org_repo_with_dashes_and_underscores() -> None:
    """Repo slugs can carry GitHub-legal characters (``-``, ``_``,
    ``.``); the builder just embeds them as-is."""
    key = build_dedup_key(
        "wf-review",
        {
            "repo": "joe-LEPPER/treadmill_v2.0",
            "pr_number": 1,
            "head_sha": "deadbeef",
        },
    )
    assert key == "wf-review:joe-LEPPER/treadmill_v2.0:pr=1,sha=deadbeef"


def test_wf_review_handles_large_pr_numbers() -> None:
    """No upper bound; the builder uses Python's int repr."""
    key = build_dedup_key(
        "wf-review",
        {
            "repo": "x/y",
            "pr_number": 999_999_999,
            "head_sha": "a" * 40,
        },
    )
    assert "pr=999999999" in key


def test_wf_review_treats_empty_string_repo_as_missing() -> None:
    """``""`` (empty string) is falsy → return None. Mirrors the
    other missing-field branches."""
    assert build_dedup_key(
        "wf-review",
        {"repo": "", "pr_number": 1, "head_sha": "abc"},
    ) is None


def test_builders_dict_does_not_include_opt_out_workflows() -> None:
    """The wf-author / wf-plan opt-out is enforced by absence from
    the registry dict — not by a None-returning builder. Codify so
    future contributors don't accidentally add a builder."""
    assert "wf-author" not in DEDUP_KEY_BUILDERS
    assert "wf-plan" not in DEDUP_KEY_BUILDERS


# ── maybe_dispatch_with_dedup: None key → unconditional dispatch ────────────


@pytest.mark.asyncio
async def test_maybe_dispatch_with_dedup_calls_dispatch_when_no_key() -> None:
    """When the builder returns None (opt-out or missing field), the
    helper falls through to ``dispatch_fn()`` unconditionally —
    existing behavior preserved."""
    run_id = uuid.uuid4()
    dispatch_fn = AsyncMock(return_value=run_id)

    # ``session`` is unused on this path (no key → no DB work).
    fake_session = AsyncMock()

    result = await maybe_dispatch_with_dedup(
        fake_session,
        workflow_id="wf-author",  # opts out
        payload={"repo": "x/y"},
        dispatch_fn=dispatch_fn,
    )
    assert result == run_id
    dispatch_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_dispatch_with_dedup_skips_when_no_key_and_dispatch_returns_none() -> None:
    """The opt-out path returns whatever ``dispatch_fn`` returns,
    including None."""
    dispatch_fn = AsyncMock(return_value=None)
    fake_session = AsyncMock()

    result = await maybe_dispatch_with_dedup(
        fake_session,
        workflow_id="wf-plan",
        payload={"repo": "x/y"},
        dispatch_fn=dispatch_fn,
    )
    assert result is None
    dispatch_fn.assert_awaited_once()


# ── maybe_dispatch_with_dedup: IntegrityError → skip ────────────────────────


class _IntegrityRaisingSession:
    """Minimal async-session stub that raises IntegrityError on
    ``flush`` (simulating a duplicate dedup_key PK collision).

    ``begin_nested()`` returns an async-context-manager that's a no-op
    on entry and re-raises whatever ``flush`` raised on exit (mirroring
    SQLAlchemy's SAVEPOINT behavior).
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.executed: list[Any] = []
        self.flush_should_raise = True

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        if self.flush_should_raise:
            raise IntegrityError("duplicate", None, Exception("PK collision"))

    async def execute(self, stmt: Any) -> Any:
        self.executed.append(stmt)
        return None

    def begin_nested(self) -> Any:
        outer = self

        class _Ctx:
            async def __aenter__(self) -> Any:
                return outer

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                # Returning False means "don't suppress"; SQLAlchemy's
                # real SAVEPOINT contextmanager would unwrap the
                # IntegrityError, but the production helper catches
                # it at the outer try/except, so propagating here
                # matches the path the helper exercises.
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_maybe_dispatch_skips_when_dedup_row_collides() -> None:
    """A duplicate dedup_key insert raises IntegrityError; the helper
    catches it, logs INFO, returns None, and does NOT call
    dispatch_fn."""
    session = _IntegrityRaisingSession()
    dispatch_fn = AsyncMock()

    result = await maybe_dispatch_with_dedup(
        session,  # type: ignore[arg-type]
        workflow_id="wf-review",
        payload={
            "repo": "x/y",
            "pr_number": 1,
            "head_sha": "deadbeef" * 5,
        },
        dispatch_fn=dispatch_fn,
    )
    assert result is None
    dispatch_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_dispatch_logs_info_on_dedup_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dedup-skip path logs at INFO with the workflow_id +
    dedup_key — operator-visible audit trail."""
    session = _IntegrityRaisingSession()
    dispatch_fn = AsyncMock()

    with caplog.at_level("INFO", logger="treadmill.coordination.dispatch_dedup"):
        await maybe_dispatch_with_dedup(
            session,  # type: ignore[arg-type]
            workflow_id="wf-review",
            payload={
                "repo": "x/y",
                "pr_number": 1,
                "head_sha": "deadbeef" * 5,
            },
            dispatch_fn=dispatch_fn,
        )
    assert any(
        "skipping duplicate" in rec.message and "wf-review" in rec.message
        for rec in caplog.records
    )


# ── maybe_dispatch_with_dedup: success path ─────────────────────────────────


class _SuccessSession:
    """Async-session stub where ``flush`` succeeds; records every
    add / execute call so tests can assert ordering."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.executed: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        self.executed.append(stmt)
        return None

    def begin_nested(self) -> Any:
        class _Ctx:
            async def __aenter__(self) -> Any:
                return None

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_maybe_dispatch_succeeds_inserts_row_and_backfills_run_id() -> None:
    """Happy path: insert succeeds, dispatch returns a run id, helper
    backfills the dedup row's workflow_run_id via UPDATE."""
    session = _SuccessSession()
    run_id = uuid.uuid4()
    dispatch_fn = AsyncMock(return_value=run_id)

    result = await maybe_dispatch_with_dedup(
        session,  # type: ignore[arg-type]
        workflow_id="wf-review",
        payload={
            "repo": "x/y",
            "pr_number": 1,
            "head_sha": "deadbeef" * 5,
        },
        dispatch_fn=dispatch_fn,
    )
    assert result == run_id
    dispatch_fn.assert_awaited_once()
    # The dedup row was added (insert-first).
    assert len(session.added) == 1
    added = session.added[0]
    assert added.dedup_key == "wf-review:x/y:pr=1,sha=" + "deadbeef" * 5
    # An UPDATE was executed to backfill workflow_run_id.
    assert len(session.executed) == 1


@pytest.mark.asyncio
async def test_maybe_dispatch_skips_update_when_dispatch_returns_none() -> None:
    """If ``dispatch_fn`` returns None (e.g. no workflow version
    seeded), the dedup row stays with the sentinel run_id — no UPDATE
    fires."""
    session = _SuccessSession()
    dispatch_fn = AsyncMock(return_value=None)

    result = await maybe_dispatch_with_dedup(
        session,  # type: ignore[arg-type]
        workflow_id="wf-review",
        payload={
            "repo": "x/y",
            "pr_number": 1,
            "head_sha": "deadbeef" * 5,
        },
        dispatch_fn=dispatch_fn,
    )
    assert result is None
    # The dedup row was inserted but no UPDATE fired.
    assert len(session.added) == 1
    assert len(session.executed) == 0
