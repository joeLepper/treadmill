"""Unit tests for wf-author no-diff routing to wf-architecture-resolve.

When a wf-author step.failed carries the no-changes error signature
("Claude Code produced no changes to commit"), the trigger must dispatch
wf-architecture-resolve — NOT wf-feedback. The no-diff case means no PR
was ever opened, so wf-feedback has nothing to remediate; the architect
reviews the task spec and emits amend/supersede/accept-as-is.

Other step.failed shapes (worker crash, author-validations rejected)
continue to route to wf-feedback as before.

Dedup coverage:
  - The dedup key is keyed on the wf-author run id, not the step id.
  - Namespace: ``wf-architecture-resolve:<repo>:author-no-diff-run=<run_id>``
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination.triggers import (
    maybe_dispatch_architect_on_author_no_diff,
    maybe_dispatch_feedback_on_step_failed,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_row(
    *,
    workflow_id: str = "wf-author",
    run_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    repo: str = "example/repo",
    step_error: str | None = None,
) -> MagicMock:
    row = MagicMock()
    row.workflow_id = workflow_id
    row.run_id = run_id or uuid.uuid4()
    row.task_id = task_id or uuid.uuid4()
    row.repo = repo
    row.step_error = step_error
    return row


def _make_session(
    row: Any,
    cap_count: int = 0,
    task: Any = None,
) -> AsyncMock:
    """Stub session for helpers that do: (1) step query, (2) cap count query,
    (3) session.get(Task, ...).
    """
    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    r2 = MagicMock()
    r2.scalar_one.return_value = cap_count
    session.execute = AsyncMock(side_effect=[r1, r2])
    session.get = AsyncMock(return_value=task)
    return session


def _fake_task(task_id: uuid.UUID, repo: str = "example/repo") -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.repo = repo
    return t


# ── maybe_dispatch_feedback_on_step_failed routing ───────────────────────────


@pytest.mark.asyncio
async def test_no_diff_error_dispatches_architect_not_feedback() -> None:
    """A wf-author step.failed with the no-changes error must dispatch
    wf-architecture-resolve, NOT wf-feedback."""
    step_id = str(uuid.uuid4())
    run_id = uuid.uuid4()
    task_id = uuid.uuid4()

    row = _make_row(
        run_id=run_id,
        task_id=task_id,
        step_error="Claude Code produced no changes to commit",
    )
    # maybe_dispatch_feedback_on_step_failed does query + error check,
    # then delegates to maybe_dispatch_architect_on_author_no_diff (own query).
    # We patch the architect helper to avoid its inner DB calls.
    architect_calls: list[dict] = []

    async def _stub_architect(
        session: Any,
        dispatcher: Any,
        *,
        step_id: str,
        workflow_id: str,
    ) -> uuid.UUID:
        architect_calls.append({"step_id": step_id, "workflow_id": workflow_id})
        return uuid.uuid4()

    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    session.execute = AsyncMock(return_value=r1)
    session.get = AsyncMock(return_value=None)

    dispatcher = MagicMock()

    with patch(
        "treadmill_api.coordination.triggers.maybe_dispatch_architect_on_author_no_diff",
        side_effect=_stub_architect,
    ):
        result = await maybe_dispatch_feedback_on_step_failed(
            session,
            dispatcher,
            step_id=step_id,
            workflow_id="wf-author",
        )

    assert len(architect_calls) == 1, (
        "no-diff error must route to maybe_dispatch_architect_on_author_no_diff"
    )
    assert architect_calls[0]["step_id"] == step_id
    assert architect_calls[0]["workflow_id"] == "wf-author"
    assert result is not None


@pytest.mark.asyncio
async def test_no_diff_error_does_not_dispatch_feedback() -> None:
    """Confirming the negative: the wf-feedback path must NOT be taken
    when the error contains the no-changes signature."""
    step_id = str(uuid.uuid4())
    row = _make_row(
        step_error="Claude Code produced no changes to commit",
    )

    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    session.execute = AsyncMock(return_value=r1)

    feedback_dispatched: list[str] = []

    async def _stub_architect(*args: Any, **kwargs: Any) -> uuid.UUID:
        return uuid.uuid4()

    async def _stub_dedup(
        session: Any,
        *,
        workflow_id: str,
        payload: Any,
        dispatch_fn: Any,
    ) -> uuid.UUID:
        feedback_dispatched.append(workflow_id)
        return uuid.uuid4()

    dispatcher = MagicMock()

    with (
        patch(
            "treadmill_api.coordination.triggers.maybe_dispatch_architect_on_author_no_diff",
            side_effect=_stub_architect,
        ),
        patch(
            "treadmill_api.coordination.triggers.maybe_dispatch_with_dedup",
            side_effect=_stub_dedup,
        ),
    ):
        await maybe_dispatch_feedback_on_step_failed(
            session,
            dispatcher,
            step_id=step_id,
            workflow_id="wf-author",
        )

    assert "wf-feedback" not in feedback_dispatched, (
        "wf-feedback must NOT be dispatched for the no-diff error shape"
    )


@pytest.mark.asyncio
async def test_other_error_dispatches_feedback_not_architect() -> None:
    """A wf-author step.failed with any other error (worker crash, etc.)
    still dispatches wf-feedback — regression guard for the existing path."""
    step_id = str(uuid.uuid4())
    run_id = uuid.uuid4()
    task_id = uuid.uuid4()
    task = _fake_task(task_id)

    row = _make_row(
        run_id=run_id,
        task_id=task_id,
        step_error="worker terminated: OOM killed",
    )

    dispatched: list[str] = []

    async def _stub_dedup(
        session: Any,
        *,
        workflow_id: str,
        payload: Any,
        dispatch_fn: Any,
    ) -> uuid.UUID:
        dispatched.append(workflow_id)
        return uuid.uuid4()

    session = _make_session(row=row, cap_count=0, task=task)
    dispatcher = MagicMock()

    with patch(
        "treadmill_api.coordination.triggers.maybe_dispatch_with_dedup",
        side_effect=_stub_dedup,
    ):
        await maybe_dispatch_feedback_on_step_failed(
            session,
            dispatcher,
            step_id=step_id,
            workflow_id="wf-author",
        )

    assert "wf-feedback" in dispatched, (
        "non-no-diff step.failed must still dispatch wf-feedback"
    )
    assert "wf-architecture-resolve" not in dispatched


@pytest.mark.asyncio
async def test_null_error_dispatches_feedback() -> None:
    """A step.failed with no error text (None) routes to wf-feedback,
    not wf-architecture-resolve."""
    step_id = str(uuid.uuid4())
    run_id = uuid.uuid4()
    task_id = uuid.uuid4()
    task = _fake_task(task_id)

    row = _make_row(run_id=run_id, task_id=task_id, step_error=None)

    dispatched: list[str] = []

    async def _stub_dedup(
        session: Any,
        *,
        workflow_id: str,
        payload: Any,
        dispatch_fn: Any,
    ) -> uuid.UUID:
        dispatched.append(workflow_id)
        return uuid.uuid4()

    session = _make_session(row=row, cap_count=0, task=task)
    dispatcher = MagicMock()

    with patch(
        "treadmill_api.coordination.triggers.maybe_dispatch_with_dedup",
        side_effect=_stub_dedup,
    ):
        await maybe_dispatch_feedback_on_step_failed(
            session,
            dispatcher,
            step_id=step_id,
            workflow_id="wf-author",
        )

    assert "wf-feedback" in dispatched


@pytest.mark.asyncio
async def test_non_wf_author_workflow_skips() -> None:
    """maybe_dispatch_feedback_on_step_failed short-circuits on
    non-wf-author workflows without hitting the DB."""
    session = AsyncMock()

    result = await maybe_dispatch_feedback_on_step_failed(
        session,
        None,  # type: ignore[arg-type]
        step_id=str(uuid.uuid4()),
        workflow_id="wf-ci-fix",
    )

    assert result is None
    session.execute.assert_not_awaited()


# ── maybe_dispatch_architect_on_author_no_diff unit tests ────────────────────


@pytest.mark.asyncio
async def test_architect_helper_skips_non_wf_author() -> None:
    """The helper must short-circuit for non-wf-author workflows — the
    no-diff route is exclusive to wf-author steps."""
    session = AsyncMock()

    result = await maybe_dispatch_architect_on_author_no_diff(
        session,
        None,  # type: ignore[arg-type]
        step_id=str(uuid.uuid4()),
        workflow_id="wf-feedback",
    )

    assert result is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_architect_helper_skips_when_error_not_no_diff() -> None:
    """The helper must return None if the step error is not the
    no-changes signature — it is only the predicate for author-no-diff."""
    step_id = str(uuid.uuid4())
    row = _make_row(step_error="validation check failed: missing tests")

    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    session.execute = AsyncMock(return_value=r1)

    result = await maybe_dispatch_architect_on_author_no_diff(
        session,
        None,  # type: ignore[arg-type]
        step_id=step_id,
        workflow_id="wf-author",
    )

    assert result is None


@pytest.mark.asyncio
async def test_architect_helper_skips_when_error_is_none() -> None:
    """None error (step.failed with no message) does not trigger the
    architect — no-diff is a specific known signature."""
    step_id = str(uuid.uuid4())
    row = _make_row(step_error=None)

    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    session.execute = AsyncMock(return_value=r1)

    result = await maybe_dispatch_architect_on_author_no_diff(
        session,
        None,  # type: ignore[arg-type]
        step_id=step_id,
        workflow_id="wf-author",
    )

    assert result is None


@pytest.mark.asyncio
async def test_architect_helper_dispatches_on_no_diff_error() -> None:
    """Happy path: when the step error contains the no-changes signature,
    dispatch wf-architecture-resolve with the author-no-diff-run dedup key."""
    step_id = str(uuid.uuid4())
    run_id = uuid.uuid4()
    task_id = uuid.uuid4()
    task = _fake_task(task_id)

    row = _make_row(
        run_id=run_id,
        task_id=task_id,
        step_error="Claude Code produced no changes to commit",
    )

    dispatched: list[dict] = []

    async def _stub_dedup(
        session: Any,
        *,
        workflow_id: str,
        payload: Any,
        dispatch_fn: Any,
    ) -> uuid.UUID:
        dispatched.append({"workflow_id": workflow_id, "payload": payload})
        return uuid.uuid4()

    session = _make_session(row=row, cap_count=0, task=task)
    dispatcher = MagicMock()

    with patch(
        "treadmill_api.coordination.triggers.maybe_dispatch_with_dedup",
        side_effect=_stub_dedup,
    ):
        result = await maybe_dispatch_architect_on_author_no_diff(
            session,
            dispatcher,
            step_id=step_id,
            workflow_id="wf-author",
        )

    assert len(dispatched) == 1
    assert dispatched[0]["workflow_id"] == "wf-architecture-resolve"
    assert dispatched[0]["payload"]["author_no_diff_run_id"] == str(run_id)
    assert result is not None


@pytest.mark.asyncio
async def test_architect_helper_respects_cap() -> None:
    """The architect helper must skip when wf-architecture-resolve is
    already at its cap for this task."""
    step_id = str(uuid.uuid4())
    run_id = uuid.uuid4()
    task_id = uuid.uuid4()

    row = _make_row(
        run_id=run_id,
        task_id=task_id,
        step_error="Claude Code produced no changes to commit",
    )

    from treadmill_api.coordination.triggers import ARCHITECTURE_RESOLVE_MAX_ATTEMPTS

    session = _make_session(row=row, cap_count=ARCHITECTURE_RESOLVE_MAX_ATTEMPTS)
    dispatcher = MagicMock()

    result = await maybe_dispatch_architect_on_author_no_diff(
        session,
        dispatcher,
        step_id=step_id,
        workflow_id="wf-author",
    )

    assert result is None


@pytest.mark.asyncio
async def test_architect_helper_skips_wrong_workflow_in_db() -> None:
    """If the DB row's workflow_id doesn't match wf-author (shouldn't
    happen in production, but defensive check), the helper returns None."""
    step_id = str(uuid.uuid4())
    row = _make_row(
        workflow_id="wf-feedback",  # wrong
        step_error="Claude Code produced no changes to commit",
    )

    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    session.execute = AsyncMock(return_value=r1)

    result = await maybe_dispatch_architect_on_author_no_diff(
        session,
        None,  # type: ignore[arg-type]
        step_id=step_id,
        workflow_id="wf-author",
    )

    assert result is None


# ── Dedup key shape ──────────────────────────────────────────────────────────


def test_author_no_diff_dedup_key_shape() -> None:
    """The dedup key for the author-no-diff trigger uses the
    ``author-no-diff-run=<wf_author_run_id>`` namespace so re-delivery
    of the same step.failed cannot create duplicate architecture-resolve
    runs for the same wf-author run."""
    from treadmill_api.coordination.dispatch_dedup import build_dedup_key

    run_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    key = build_dedup_key(
        "wf-architecture-resolve",
        {
            "repo": "example/repo",
            "author_no_diff_run_id": run_id,
        },
    )
    assert key == f"wf-architecture-resolve:example/repo:author-no-diff-run={run_id}"


def test_dedup_key_keyed_on_run_id_not_step_id() -> None:
    """The dedup key discriminator is the wf-author RUN id, not the step
    id — two step.failed events for different steps of the same run
    would produce the same key (dedup suppresses the second dispatch),
    but two different runs always get distinct keys."""
    from treadmill_api.coordination.dispatch_dedup import build_dedup_key

    run_id_a = str(uuid.uuid4())
    run_id_b = str(uuid.uuid4())

    key_a = build_dedup_key(
        "wf-architecture-resolve",
        {"repo": "example/repo", "author_no_diff_run_id": run_id_a},
    )
    key_b = build_dedup_key(
        "wf-architecture-resolve",
        {"repo": "example/repo", "author_no_diff_run_id": run_id_b},
    )

    assert key_a != key_b, "different run ids must produce different dedup keys"
    assert "author-no-diff-run=" in (key_a or "")
    assert run_id_a in (key_a or "")


def test_deadlock_feedback_key_still_works() -> None:
    """Regression: existing deadlock-feedback-run dedup key must still
    be built correctly after the builder was extended for author-no-diff."""
    from treadmill_api.coordination.dispatch_dedup import build_dedup_key

    run_id = "b2c3d4e5-f6a7-8901-bcde-f01234567890"
    key = build_dedup_key(
        "wf-architecture-resolve",
        {
            "repo": "example/repo",
            "deadlock_feedback_run_id": run_id,
        },
    )
    assert key == f"wf-architecture-resolve:example/repo:deadlock-feedback-run={run_id}"
