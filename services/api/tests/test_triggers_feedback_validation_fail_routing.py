"""Unit tests for the wf-feedback validation-fail → wf-architecture-resolve
trigger (ADR-0048 follow-on, 2026-05-19).

Sibling of ``test_triggers_author_no_diff_routing.py`` — different trigger
source (wf-feedback's action step vs wf-author's no-changes step.failed).

Predicate:
  * step belongs to wf-feedback
  * step_name == 'action'
  * output.decision == 'fail'
  * output.payload.validation_results contains at least one verdict='fail'
  * task has no open PR
  * existing cap on wf-architecture-resolve applies

Dedup namespace:
  ``wf-architecture-resolve:<repo>:feedback-validation-fail-step=<step_id>``
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treadmill_api.coordination.triggers import (
    ARCHITECTURE_RESOLVE_MAX_ATTEMPTS,
    maybe_dispatch_architect_on_feedback_validation_fail,
)
from treadmill_api.events.step import StepCompleted
from treadmill_api.events.step_output import Metadata, StepOutput


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_typed(
    *,
    decision: str = "fail",
    validation_results: list[dict[str, Any]] | None = None,
    payload_extra: dict[str, Any] | None = None,
) -> StepCompleted:
    """Build a StepCompleted with the validation-fail payload shape."""
    payload: dict[str, Any] = {}
    if validation_results is not None:
        payload["validation_results"] = validation_results
    if payload_extra:
        payload.update(payload_extra)
    output = StepOutput(
        summary="Author-side validation failed",
        decision=decision,
        commit_sha=None,
        artifacts=[],
        payload=payload,
        metadata=Metadata(),
    )
    return StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=output,
    )


def _failing_validation_results() -> list[dict[str, Any]]:
    return [
        {
            "check_id": "tests-must-exist",
            "kind": "deterministic",
            "verdict": "fail",
            "rationale": "no tests in diff",
            "log_excerpt": "+ pytest -q\nERROR: no tests found",
        },
    ]


def _make_row(
    *,
    workflow_id: str = "wf-feedback",
    step_name: str = "action",
    run_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    repo: str = "example/repo",
) -> MagicMock:
    row = MagicMock()
    row.workflow_id = workflow_id
    row.run_id = run_id or uuid.uuid4()
    row.task_id = task_id or uuid.uuid4()
    row.repo = repo
    row.step_name = step_name
    return row


def _fake_task(task_id: uuid.UUID, repo: str = "example/repo") -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.repo = repo
    return t


def _make_session(
    *,
    row: Any,
    open_pr_row: Any = None,
    cap_count: int = 0,
    task: Any = None,
) -> AsyncMock:
    """Stub session for the helper's three sequential queries:
      1. step → (workflow_id, run_id, task_id, repo, step_name)
      2. open-PR lookup → row with pr_number OR None
      3. cap-count → scalar_one() returns the count

    Then ``session.get(Task, ...)`` returns ``task``.
    """
    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    r2 = MagicMock()
    r2.first.return_value = open_pr_row
    r3 = MagicMock()
    r3.scalar_one.return_value = cap_count
    session.execute = AsyncMock(side_effect=[r1, r2, r3])
    session.get = AsyncMock(return_value=task)
    return session


# ── Happy path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_dispatches_architect_with_source_step_id() -> None:
    """wf-feedback action.completed with decision=fail + validation_results
    containing a fail verdict + no open PR for the task → dispatch
    wf-architecture-resolve with source_step_id set to the action step."""
    step_id = str(uuid.uuid4())
    task_id = uuid.uuid4()
    task = _fake_task(task_id)
    row = _make_row(task_id=task_id)
    typed = _make_typed(validation_results=_failing_validation_results())

    dispatched: list[dict[str, Any]] = []

    async def _stub_dedup(
        session: Any,
        *,
        workflow_id: str,
        payload: Any,
        dispatch_fn: Any,
    ) -> uuid.UUID:
        dispatched.append({"workflow_id": workflow_id, "payload": payload})
        # Execute the dispatch_fn so we can capture source_step_id.
        return await dispatch_fn()

    captured_call: dict[str, Any] = {}

    async def _stub_create(
        session: Any,
        dispatcher: Any,
        *,
        task: Any,
        workflow_id: str,
        trigger: str,
        source_step_id: Any = None,
    ) -> uuid.UUID:
        captured_call["workflow_id"] = workflow_id
        captured_call["trigger"] = trigger
        captured_call["source_step_id"] = source_step_id
        return uuid.uuid4()

    session = _make_session(row=row, cap_count=0, task=task)
    dispatcher = MagicMock()

    with (
        patch(
            "treadmill_api.coordination.triggers.maybe_dispatch_with_dedup",
            side_effect=_stub_dedup,
        ),
        patch(
            "treadmill_api.coordination.triggers._create_and_publish_run",
            side_effect=_stub_create,
        ),
    ):
        result = await maybe_dispatch_architect_on_feedback_validation_fail(
            session, dispatcher, step_id=step_id, typed=typed,
        )

    assert result is not None
    assert len(dispatched) == 1
    assert dispatched[0]["workflow_id"] == "wf-architecture-resolve"
    assert dispatched[0]["payload"]["feedback_validation_fail_step_id"] == step_id
    assert dispatched[0]["payload"]["repo"] == "example/repo"
    assert captured_call["workflow_id"] == "wf-architecture-resolve"
    assert captured_call["trigger"] == "self:wf-feedback-validation-fail"
    assert captured_call["source_step_id"] == step_id


# ── Predicate: decision filter ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decision_not_fail_skips() -> None:
    """A wf-feedback action step that completed with
    ``responded-without-change`` (the deadlock-arbitration trigger's case)
    must NOT dispatch through this trigger."""
    step_id = str(uuid.uuid4())
    typed = _make_typed(
        decision="responded-without-change",
        validation_results=_failing_validation_results(),
    )

    session = AsyncMock()
    dispatcher = MagicMock()

    result = await maybe_dispatch_architect_on_feedback_validation_fail(
        session, dispatcher, step_id=step_id, typed=typed,
    )

    assert result is None
    session.execute.assert_not_awaited()


# ── Predicate: payload shape filter ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_validation_results_in_payload_skips() -> None:
    """A wf-feedback action step with decision=fail but no
    ``validation_results`` field in the payload (e.g. a different
    failure code path) must NOT dispatch."""
    step_id = str(uuid.uuid4())
    typed = _make_typed(decision="fail", validation_results=None)

    session = AsyncMock()
    dispatcher = MagicMock()

    result = await maybe_dispatch_architect_on_feedback_validation_fail(
        session, dispatcher, step_id=step_id, typed=typed,
    )

    assert result is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_validation_results_pass_skips() -> None:
    """A wf-feedback action with decision=fail and validation_results
    that are all verdict=pass (shouldn't happen but defensive) does NOT
    dispatch."""
    step_id = str(uuid.uuid4())
    typed = _make_typed(
        decision="fail",
        validation_results=[
            {
                "check_id": "tests-must-exist",
                "kind": "deterministic",
                "verdict": "pass",
                "rationale": "ok",
                "log_excerpt": "",
            },
        ],
    )

    session = AsyncMock()
    dispatcher = MagicMock()

    result = await maybe_dispatch_architect_on_feedback_validation_fail(
        session, dispatcher, step_id=step_id, typed=typed,
    )

    assert result is None
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_validation_results_skips() -> None:
    """An empty validation_results list also skips (defensive)."""
    step_id = str(uuid.uuid4())
    typed = _make_typed(decision="fail", validation_results=[])

    session = AsyncMock()
    dispatcher = MagicMock()

    result = await maybe_dispatch_architect_on_feedback_validation_fail(
        session, dispatcher, step_id=step_id, typed=typed,
    )

    assert result is None
    session.execute.assert_not_awaited()


# ── Predicate: open-PR filter ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_has_open_pr_skips() -> None:
    """When the task has an open PR, defer to the deadlock-arbitration
    path. This trigger owns the no-PR case only."""
    step_id = str(uuid.uuid4())
    task_id = uuid.uuid4()
    row = _make_row(task_id=task_id)
    typed = _make_typed(validation_results=_failing_validation_results())

    # open_pr_row non-None → an open PR exists for this task.
    open_pr_row = MagicMock()
    open_pr_row.pr_number = 42

    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    r2 = MagicMock()
    r2.first.return_value = open_pr_row
    session.execute = AsyncMock(side_effect=[r1, r2])

    dispatcher = MagicMock()

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

    with patch(
        "treadmill_api.coordination.triggers.maybe_dispatch_with_dedup",
        side_effect=_stub_dedup,
    ):
        result = await maybe_dispatch_architect_on_feedback_validation_fail(
            session, dispatcher, step_id=step_id, typed=typed,
        )

    assert result is None
    assert dispatched == [], "open-PR case must not dispatch"


# ── Predicate: workflow + step_name filters ──────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_workflow_skips() -> None:
    """A step from wf-author or wf-validate with the matching payload
    shape must NOT dispatch — only wf-feedback action triggers this path."""
    step_id = str(uuid.uuid4())
    row = _make_row(workflow_id="wf-author")  # wrong
    typed = _make_typed(validation_results=_failing_validation_results())

    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    session.execute = AsyncMock(return_value=r1)

    dispatcher = MagicMock()

    result = await maybe_dispatch_architect_on_feedback_validation_fail(
        session, dispatcher, step_id=step_id, typed=typed,
    )

    assert result is None


@pytest.mark.asyncio
async def test_wrong_step_name_skips() -> None:
    """wf-feedback's analyzer step (or any other step) must NOT trigger
    this path — only the action step's decision=fail indicates a
    tried-and-rejected diff."""
    step_id = str(uuid.uuid4())
    row = _make_row(step_name="analyzer")  # wrong
    typed = _make_typed(validation_results=_failing_validation_results())

    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = row
    session.execute = AsyncMock(return_value=r1)

    dispatcher = MagicMock()

    result = await maybe_dispatch_architect_on_feedback_validation_fail(
        session, dispatcher, step_id=step_id, typed=typed,
    )

    assert result is None


# ── Cap ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cap_reached_skips_and_emits_escalation() -> None:
    """When wf-architecture-resolve has hit the per-task cap, the helper
    must skip dispatch and emit the escalation event (same shape as the
    deadlock-arbitration trigger)."""
    step_id = str(uuid.uuid4())
    task_id = uuid.uuid4()
    row = _make_row(task_id=task_id)
    typed = _make_typed(validation_results=_failing_validation_results())

    session = _make_session(
        row=row, open_pr_row=None, cap_count=ARCHITECTURE_RESOLVE_MAX_ATTEMPTS,
    )
    dispatcher = MagicMock()

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

    cap_calls: list[uuid.UUID] = []

    async def _stub_emit_cap(
        session: Any, dispatcher: Any, *, task_id: uuid.UUID, repo: str,
    ) -> None:
        cap_calls.append(task_id)

    with (
        patch(
            "treadmill_api.coordination.triggers.maybe_dispatch_with_dedup",
            side_effect=_stub_dedup,
        ),
        patch(
            "treadmill_api.coordination.triggers._emit_arch_cap_reached",
            side_effect=_stub_emit_cap,
        ),
    ):
        result = await maybe_dispatch_architect_on_feedback_validation_fail(
            session, dispatcher, step_id=step_id, typed=typed,
        )

    assert result is None
    assert dispatched == []
    assert cap_calls == [task_id]


# ── Dedup ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_suppresses_redelivery() -> None:
    """Re-delivery of the same wf-feedback action step.completed must not
    dispatch twice — the dedup key is keyed on the step id, so the
    second call returns None."""
    step_id = str(uuid.uuid4())
    task_id = uuid.uuid4()
    task = _fake_task(task_id)
    row1 = _make_row(task_id=task_id)
    row2 = _make_row(task_id=task_id)
    typed = _make_typed(validation_results=_failing_validation_results())

    # First call: dedup insert succeeds; second call: IntegrityError → None.
    call_count = {"n": 0}

    async def _stub_dedup(
        session: Any,
        *,
        workflow_id: str,
        payload: Any,
        dispatch_fn: Any,
    ) -> uuid.UUID | None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return await dispatch_fn()
        return None

    async def _stub_create(
        session: Any,
        dispatcher: Any,
        *,
        task: Any,
        workflow_id: str,
        trigger: str,
        source_step_id: Any = None,
    ) -> uuid.UUID:
        return uuid.uuid4()

    session_1 = _make_session(row=row1, cap_count=0, task=task)
    session_2 = _make_session(row=row2, cap_count=0, task=task)
    dispatcher = MagicMock()

    with (
        patch(
            "treadmill_api.coordination.triggers.maybe_dispatch_with_dedup",
            side_effect=_stub_dedup,
        ),
        patch(
            "treadmill_api.coordination.triggers._create_and_publish_run",
            side_effect=_stub_create,
        ),
    ):
        first = await maybe_dispatch_architect_on_feedback_validation_fail(
            session_1, dispatcher, step_id=step_id, typed=typed,
        )
        second = await maybe_dispatch_architect_on_feedback_validation_fail(
            session_2, dispatcher, step_id=step_id, typed=typed,
        )

    assert first is not None
    assert second is None


def test_dedup_key_shape() -> None:
    """The dedup key for the feedback-validation-fail trigger uses the
    ``feedback-validation-fail-step=<step_id>`` namespace. Distinct from
    the deadlock and author-no-diff namespaces."""
    from treadmill_api.coordination.dispatch_dedup import build_dedup_key

    step_id = "deadbeef-1111-2222-3333-444455556666"
    key = build_dedup_key(
        "wf-architecture-resolve",
        {
            "repo": "example/repo",
            "feedback_validation_fail_step_id": step_id,
        },
    )
    assert key == (
        f"wf-architecture-resolve:example/repo:"
        f"feedback-validation-fail-step={step_id}"
    )


def test_dedup_key_distinct_from_deadlock_and_no_diff() -> None:
    """The three wf-architecture-resolve trigger sources use distinct
    discriminator namespaces so they cannot collide on the dedup table."""
    from treadmill_api.coordination.dispatch_dedup import build_dedup_key

    run_id = str(uuid.uuid4())
    step_id = str(uuid.uuid4())
    repo = "example/repo"

    deadlock_key = build_dedup_key(
        "wf-architecture-resolve",
        {"repo": repo, "deadlock_feedback_run_id": run_id},
    )
    no_diff_key = build_dedup_key(
        "wf-architecture-resolve",
        {"repo": repo, "author_no_diff_run_id": run_id},
    )
    validation_fail_key = build_dedup_key(
        "wf-architecture-resolve",
        {"repo": repo, "feedback_validation_fail_step_id": step_id},
    )

    keys = {deadlock_key, no_diff_key, validation_fail_key}
    assert len(keys) == 3, "all three namespaces must produce distinct keys"
    assert "feedback-validation-fail-step=" in (validation_fail_key or "")
