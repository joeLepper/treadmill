"""Unit tests for maybe_auto_merge_on_mergeable (ADR-0031).

Covers the cooling-off deadline logic, skip conditions, poll-loop fire
behavior, and consumer wiring — all without live Postgres or Redis.
Mock session.execute returns pre-canned rows; mock redis records calls.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.triggers import (
    AUTO_MERGE_COOLDOWN_SECONDS,
    AUTO_MERGE_DEADLINE_KEY_PREFIX,
    AUTO_MERGE_FIRED_KEY_PREFIX,
    _check_still_mergeable_for_auto_merge,
    _process_deadline_key,
    fire_elapsed_auto_merges,
    maybe_auto_merge_on_mergeable,
)


# ── Fixture helpers ────────────────────────────────────────────────────────────


def _workflow_row(workflow_id: str = "wf-validate", task_id: uuid.UUID | None = None) -> MagicMock:
    row = MagicMock()
    row.workflow_id = workflow_id
    row.task_id = task_id or uuid.uuid4()
    return row


def _merge_row(
    derived_mergeability: str = "mergeable",
    validate_decision: str = "pass",
    review_decision: str = "approved",
    repo: str = "acme/repo",
    pr_number: int = 42,
    auto_merge: bool | None = None,
) -> MagicMock:
    row = MagicMock()
    row.derived_mergeability = derived_mergeability
    row.validate_decision = validate_decision
    row.review_decision = review_decision
    row.repo = repo
    row.pr_number = pr_number
    row.auto_merge = auto_merge
    return row


def _make_session(wf_row: Any, mg_row: Any) -> AsyncMock:
    """Build a mock session whose first execute returns wf_row and second mg_row."""
    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = wf_row
    r2 = MagicMock()
    r2.first.return_value = mg_row
    session.execute = AsyncMock(side_effect=[r1, r2])
    return session


def _make_redis(exists: int = 0) -> AsyncMock:
    redis = AsyncMock()
    redis.exists = AsyncMock(return_value=exists)
    redis.set = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.delete = AsyncMock(return_value=1)
    redis.scan = AsyncMock(return_value=(0, []))
    return redis


# ── maybe_auto_merge_on_mergeable: skip conditions ────────────────────────────


@pytest.mark.asyncio
async def test_skip_when_redis_client_not_wired() -> None:
    session = AsyncMock()
    result = await maybe_auto_merge_on_mergeable(session, None, step_id="step-1")
    assert result is False
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_step_not_found() -> None:
    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = None
    session.execute = AsyncMock(return_value=r1)
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="missing-step")
    assert result is False
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_workflow_not_wf_validate_or_review() -> None:
    wf = _workflow_row(workflow_id="wf-author")
    session = _make_session(wf, _merge_row())
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is False
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_already_fired() -> None:
    task_id = uuid.uuid4()
    wf = _workflow_row(workflow_id="wf-validate", task_id=task_id)
    # Session only needs one execute (for the workflow row); the fired-key
    # check happens before the mergeability query.
    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = wf
    session.execute = AsyncMock(return_value=r1)
    redis = _make_redis(exists=1)  # fired key exists

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is False
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_plan_opted_out() -> None:
    task_id = uuid.uuid4()
    wf = _workflow_row(workflow_id="wf-review", task_id=task_id)
    mg = _merge_row(auto_merge=False)
    session = _make_session(wf, mg)
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is False
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_not_mergeable() -> None:
    wf = _workflow_row(workflow_id="wf-validate")
    mg = _merge_row(derived_mergeability="blocked-on-ci")
    session = _make_session(wf, mg)
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is False
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_validate_decision_not_pass() -> None:
    """ADR-0031 Q31.b: 'uncertain' must NOT auto-merge."""
    wf = _workflow_row(workflow_id="wf-validate")
    mg = _merge_row(validate_decision="uncertain")
    session = _make_session(wf, mg)
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is False
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_pending_human_review() -> None:
    wf = _workflow_row(workflow_id="wf-validate")
    mg = _merge_row(review_decision="changes_requested")
    session = _make_session(wf, mg)
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is False
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_no_mergeability_row() -> None:
    """No task_prs row → VIEW has no row → should skip cleanly."""
    task_id = uuid.uuid4()
    wf = _workflow_row(workflow_id="wf-validate", task_id=task_id)
    session = AsyncMock()
    r1 = MagicMock()
    r1.first.return_value = wf
    r2 = MagicMock()
    r2.first.return_value = None  # no VIEW row
    session.execute = AsyncMock(side_effect=[r1, r2])
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is False
    redis.set.assert_not_awaited()


# ── maybe_auto_merge_on_mergeable: happy paths ────────────────────────────────


@pytest.mark.asyncio
async def test_sets_deadline_when_wf_validate_passes_all_conditions() -> None:
    task_id = uuid.uuid4()
    wf = _workflow_row(workflow_id="wf-validate", task_id=task_id)
    mg = _merge_row()  # fully mergeable defaults
    session = _make_session(wf, mg)
    redis = _make_redis()

    before = datetime.now(timezone.utc)
    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    after = datetime.now(timezone.utc)

    assert result is True
    redis.set.assert_awaited_once()

    # Verify the key and deadline window.
    call_args = redis.set.await_args
    key_arg = call_args[0][0]
    value_arg = call_args[0][1]
    assert key_arg == AUTO_MERGE_DEADLINE_KEY_PREFIX + str(task_id)

    data = json.loads(value_arg)
    assert data["task_id"] == str(task_id)
    assert data["repo"] == "acme/repo"
    assert data["pr_number"] == 42
    deadline = datetime.fromisoformat(data["deadline_at"])
    assert before + timedelta(seconds=AUTO_MERGE_COOLDOWN_SECONDS) <= deadline
    assert deadline <= after + timedelta(seconds=AUTO_MERGE_COOLDOWN_SECONDS)


@pytest.mark.asyncio
async def test_sets_deadline_when_wf_review_passes_all_conditions() -> None:
    wf = _workflow_row(workflow_id="wf-review")
    mg = _merge_row()
    session = _make_session(wf, mg)
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is True
    redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_deadline_pushed_on_second_call() -> None:
    """Two consecutive calls both set the deadline (push forward)."""
    task_id = uuid.uuid4()
    wf = _workflow_row(workflow_id="wf-validate", task_id=task_id)
    mg = _merge_row()

    # First call
    session1 = _make_session(wf, mg)
    redis = _make_redis()
    await maybe_auto_merge_on_mergeable(session1, redis, step_id="step-1")

    # Second call (e.g. wf-review also completed shortly after)
    session2 = _make_session(_workflow_row(workflow_id="wf-review", task_id=task_id), mg)
    await maybe_auto_merge_on_mergeable(session2, redis, step_id="step-2")

    assert redis.set.await_count == 2


@pytest.mark.asyncio
async def test_plan_auto_merge_none_is_not_opted_out() -> None:
    """NULL (Python None) plan.auto_merge means enabled, not opted out."""
    wf = _workflow_row(workflow_id="wf-validate")
    mg = _merge_row(auto_merge=None)
    session = _make_session(wf, mg)
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is True


@pytest.mark.asyncio
async def test_plan_auto_merge_true_is_not_opted_out() -> None:
    """Explicit True plan.auto_merge means enabled."""
    wf = _workflow_row(workflow_id="wf-validate")
    mg = _merge_row(auto_merge=True)
    session = _make_session(wf, mg)
    redis = _make_redis()

    result = await maybe_auto_merge_on_mergeable(session, redis, step_id="step-1")
    assert result is True


# ── fire_elapsed_auto_merges: poll-loop behavior ───────────────────────────────


def _make_deadline_value(
    task_id: uuid.UUID,
    repo: str = "acme/repo",
    pr_number: int = 42,
    deadline_offset_seconds: float = -5,  # negative = elapsed
) -> bytes:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=deadline_offset_seconds)
    return json.dumps({
        "task_id": str(task_id),
        "repo": repo,
        "pr_number": pr_number,
        "deadline_at": deadline.isoformat(),
    }).encode()


def _make_sessionmaker(check_result: bool = True) -> Any:
    """Build a stub sessionmaker that returns check_result from the re-verify."""
    session = AsyncMock()
    r = MagicMock()
    r.first.return_value = MagicMock(
        derived_mergeability="mergeable" if check_result else "blocked-on-ci",
        auto_merge=None,
    )
    session.execute = AsyncMock(return_value=r)

    @asynccontextmanager
    async def _cm():
        yield session

    def _make():
        return _cm()

    return _make


@pytest.mark.asyncio
async def test_fire_elapsed_auto_merges_skips_when_redis_none() -> None:
    result = await fire_elapsed_auto_merges(None, None, MagicMock())
    assert result == 0


@pytest.mark.asyncio
async def test_fire_elapsed_auto_merges_skips_when_github_none() -> None:
    redis = _make_redis()
    result = await fire_elapsed_auto_merges(redis, None, None)
    assert result == 0


@pytest.mark.asyncio
async def test_fire_elapsed_auto_merges_fires_when_deadline_elapsed() -> None:
    task_id = uuid.uuid4()
    key = (AUTO_MERGE_DEADLINE_KEY_PREFIX + str(task_id)).encode()
    value = _make_deadline_value(task_id, deadline_offset_seconds=-10)

    redis = _make_redis()
    redis.scan = AsyncMock(return_value=(0, [key]))
    redis.get = AsyncMock(return_value=value)

    github = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    github.put = AsyncMock(return_value=response)

    sm = _make_sessionmaker(check_result=True)

    fired = await fire_elapsed_auto_merges(redis, sm, github)
    assert fired == 1
    github.put.assert_awaited_once()
    call_args = github.put.await_args
    assert "/repos/acme/repo/pulls/42/merge" in call_args[0][0]
    assert call_args[1]["json"] == {"merge_method": "squash"}

    # Fired key set, deadline key deleted.
    assert redis.set.await_count == 1
    set_call = redis.set.await_args
    assert set_call[0][0] == AUTO_MERGE_FIRED_KEY_PREFIX + str(task_id)
    redis.delete.assert_awaited_once_with(key)


@pytest.mark.asyncio
async def test_fire_elapsed_auto_merges_skips_when_deadline_future() -> None:
    task_id = uuid.uuid4()
    key = (AUTO_MERGE_DEADLINE_KEY_PREFIX + str(task_id)).encode()
    value = _make_deadline_value(task_id, deadline_offset_seconds=+20)  # not yet

    redis = _make_redis()
    redis.scan = AsyncMock(return_value=(0, [key]))
    redis.get = AsyncMock(return_value=value)

    github = AsyncMock()
    sm = _make_sessionmaker()

    fired = await fire_elapsed_auto_merges(redis, sm, github)
    assert fired == 0
    github.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_fire_elapsed_auto_merges_clears_key_when_no_longer_mergeable() -> None:
    task_id = uuid.uuid4()
    key = (AUTO_MERGE_DEADLINE_KEY_PREFIX + str(task_id)).encode()
    value = _make_deadline_value(task_id, deadline_offset_seconds=-5)

    redis = _make_redis()
    redis.scan = AsyncMock(return_value=(0, [key]))
    redis.get = AsyncMock(return_value=value)

    github = AsyncMock()
    sm = _make_sessionmaker(check_result=False)  # no longer mergeable

    fired = await fire_elapsed_auto_merges(redis, sm, github)
    assert fired == 0
    github.put.assert_not_awaited()
    redis.delete.assert_awaited_once_with(key)


@pytest.mark.asyncio
async def test_fire_elapsed_auto_merges_retries_on_github_error() -> None:
    """A GitHub API failure leaves the deadline key; the next tick retries."""
    task_id = uuid.uuid4()
    key = (AUTO_MERGE_DEADLINE_KEY_PREFIX + str(task_id)).encode()
    value = _make_deadline_value(task_id, deadline_offset_seconds=-5)

    redis = _make_redis()
    redis.scan = AsyncMock(return_value=(0, [key]))
    redis.get = AsyncMock(return_value=value)

    github = AsyncMock()
    github.put = AsyncMock(side_effect=RuntimeError("network error"))

    sm = _make_sessionmaker(check_result=True)

    fired = await fire_elapsed_auto_merges(redis, sm, github)
    assert fired == 0
    redis.delete.assert_not_awaited()  # key not deleted on failure


@pytest.mark.asyncio
async def test_fire_elapsed_auto_merges_skips_malformed_key() -> None:
    """Malformed JSON in a deadline key results in 0 merges fired and key deleted."""
    task_id = uuid.uuid4()
    key = (AUTO_MERGE_DEADLINE_KEY_PREFIX + str(task_id)).encode()
    redis = _make_redis()
    redis.scan = AsyncMock(return_value=(0, [key]))
    redis.get = AsyncMock(return_value=b"{bad json")

    github = AsyncMock()
    fired = await fire_elapsed_auto_merges(redis, _make_sessionmaker(), github)
    assert fired == 0
    redis.delete.assert_awaited_once_with(key)


@pytest.mark.asyncio
async def test_process_deadline_key_deletes_malformed_json() -> None:
    """_process_deadline_key cleans up unreadable keys."""
    key = b"treadmill:auto-merge-deadline:bad"
    redis = _make_redis()
    redis.get = AsyncMock(return_value=b"{bad json")

    result = await _process_deadline_key(
        redis_client=redis,
        sessionmaker=_make_sessionmaker(),
        github_client=AsyncMock(),
        raw_key=key,
        now=datetime.now(timezone.utc),
    )
    assert result is False
    redis.delete.assert_awaited_once_with(key)


# ── _check_still_mergeable_for_auto_merge ─────────────────────────────────────


@pytest.mark.asyncio
async def test_check_still_mergeable_returns_true_when_mergeable() -> None:
    session = AsyncMock()
    r = MagicMock()
    r.first.return_value = MagicMock(derived_mergeability="mergeable", auto_merge=None)
    session.execute = AsyncMock(return_value=r)

    result = await _check_still_mergeable_for_auto_merge(session, uuid.uuid4())
    assert result is True


@pytest.mark.asyncio
async def test_check_still_mergeable_returns_false_when_no_row() -> None:
    session = AsyncMock()
    r = MagicMock()
    r.first.return_value = None
    session.execute = AsyncMock(return_value=r)

    result = await _check_still_mergeable_for_auto_merge(session, uuid.uuid4())
    assert result is False


@pytest.mark.asyncio
async def test_check_still_mergeable_returns_false_when_opted_out() -> None:
    session = AsyncMock()
    r = MagicMock()
    r.first.return_value = MagicMock(derived_mergeability="mergeable", auto_merge=False)
    session.execute = AsyncMock(return_value=r)

    result = await _check_still_mergeable_for_auto_merge(session, uuid.uuid4())
    assert result is False


@pytest.mark.asyncio
async def test_check_still_mergeable_returns_false_when_blocked() -> None:
    session = AsyncMock()
    r = MagicMock()
    r.first.return_value = MagicMock(derived_mergeability="blocked-on-ci", auto_merge=None)
    session.execute = AsyncMock(return_value=r)

    result = await _check_still_mergeable_for_auto_merge(session, uuid.uuid4())
    assert result is False


# ── Consumer wiring ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consumer_calls_maybe_fire_auto_merge_on_step_completed() -> None:
    """Consumer's handle() must invoke _maybe_fire_auto_merge on step.completed."""
    from contextlib import asynccontextmanager

    from treadmill_api.coordination.consumer import CoordinationConsumer

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(
        scalars=MagicMock(return_value=[]),
        scalar_one_or_none=MagicMock(return_value=None),
        first=MagicMock(return_value=None),
        all=MagicMock(return_value=[]),
    ))

    @asynccontextmanager
    async def _cm():
        yield session

    def _sm():
        return _cm()

    calls: list[dict] = []

    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_sm,  # type: ignore[arg-type]
        redis_client=AsyncMock(),
    )

    async def _stub_auto_merge(sess, step_id):
        calls.append({"step_id": step_id})

    consumer._maybe_fire_auto_merge = _stub_auto_merge  # type: ignore[method-assign]

    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-14T10:00:00+00:00",
            "output": {
                "summary": "done",
                "decision": "pass",
                "commit_sha": "abc123",
                "artifacts": [],
                "payload": {},
                "metadata": {},
            },
        },
    })

    assert len(calls) == 1, (
        "consumer must call _maybe_fire_auto_merge once for step.completed"
    )


@pytest.mark.asyncio
async def test_consumer_skips_auto_merge_when_redis_not_wired() -> None:
    """When redis_client is None, _maybe_fire_auto_merge skips cleanly."""
    from contextlib import asynccontextmanager
    from treadmill_api.coordination.consumer import CoordinationConsumer

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(
        first=MagicMock(return_value=None),
    ))

    @asynccontextmanager
    async def _cm():
        yield session

    def _sm():
        return _cm()

    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_sm,  # type: ignore[arg-type]
        redis_client=None,  # not wired
    )

    trigger_calls: list[int] = []

    async def _stub(*args, **kwargs) -> None:
        trigger_calls.append(1)

    import treadmill_api.coordination.triggers as _triggers
    original = _triggers.maybe_auto_merge_on_mergeable
    _triggers.maybe_auto_merge_on_mergeable = _stub  # type: ignore[assignment]
    try:
        await consumer._maybe_fire_auto_merge(session, "step-id-1")
    finally:
        _triggers.maybe_auto_merge_on_mergeable = original  # type: ignore[assignment]

    assert trigger_calls == [], (
        "maybe_auto_merge_on_mergeable must not be called when redis_client is None"
    )


# ── Constants ──────────────────────────────────────────────────────────────────


def test_auto_merge_cooldown_is_30_seconds() -> None:
    """Per ADR-0031 Q31.a: cooling-off window is exactly 30 seconds."""
    assert AUTO_MERGE_COOLDOWN_SECONDS == 30, (
        "ADR-0031 Q31.a specifies a 30-second cooling-off window"
    )


def test_auto_merge_deadline_key_prefix() -> None:
    assert AUTO_MERGE_DEADLINE_KEY_PREFIX == "treadmill:auto-merge-deadline:"


def test_auto_merge_fired_key_prefix() -> None:
    assert AUTO_MERGE_FIRED_KEY_PREFIX == "treadmill:auto-merge-fired:"
