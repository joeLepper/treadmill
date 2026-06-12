"""Unit tests for ``treadmill_api.webhooks.persist``.

Per ADR-0063 Step 3 the shared
``persist_and_resolve_webhook_event(session, normalized, body_json,
redis_client, publisher) -> Event`` helper is the single seam both
webhook ingress paths (HTTP route + SQS poller) use for FK resolution +
buffer-on-miss + publish. These tests pin its contract:

* task_prs hit → Event row carries the resolved ``task_id``; no buffer.
* task_prs miss → Event row carries ``task_id=NULL``; buffer pushed.
* No (repo, pr_number) on the normalized event → no task_prs lookup;
  no buffer; Event still persisted.
* No redis_client → buffer skipped cleanly (narrow tests + log-only
  deployments).
* Publish failure → logged + swallowed (the Event row is the source of
  truth; consumer rescan recovers).
* ValidationError from the typed registry propagates (signals a
  normalizer ↔ registry drift; the caller picks the status code).
* Deterministic ``event_id`` → upsert path (``ON CONFLICT DO NOTHING``);
  default → fresh database-generated UUID.

The session is stubbed because the test focuses on the helper's call
graph; an integration test in ``test_integration_webhook_inbox.py``
exercises the full Postgres + Redis chain via the SQS poller.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.webhooks.normalize import NormalizationResult
from treadmill_api.webhooks.persist import persist_and_resolve_webhook_event


# ── Stubs ─────────────────────────────────────────────────────────────────────


class _StubPublisher:
    """Records publish calls so the tests can assert fan-out happened."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.published: list[tuple[Any, Any]] = []
        self._raises = raises

    async def publish(self, event: Any, typed: Any) -> None:
        self.published.append((event, typed))
        if self._raises is not None:
            raise self._raises


class _StubRedis:
    """Minimal async Redis stub recording rpush + expire calls.

    Mirrors the surface ``buffer_pending_event`` touches; the helper
    drives it through ``buffer_pending_event``, so a captured rpush is
    evidence the buffer branch fired.
    """

    def __init__(self, raises_on_rpush: BaseException | None = None) -> None:
        self.rpush_calls: list[tuple[str, Any]] = []
        self.expire_calls: list[tuple[str, int]] = []
        self._raises = raises_on_rpush

    async def rpush(self, key: str, value: Any) -> int:
        self.rpush_calls.append((key, value))
        if self._raises is not None:
            raise self._raises
        return len(self.rpush_calls)

    async def expire(self, key: str, seconds: int) -> bool:
        self.expire_calls.append((key, seconds))
        return True


def _normalized_pr_opened(
    *,
    repo: str = "joe/treadmill",
    pr_number: int | None = 42,
) -> NormalizationResult:
    """Build a NormalizationResult for a pr_opened event.

    The payload shape matches ``GitHubPROpened`` in the event registry so
    the helper's typed-payload validation passes.
    """
    return NormalizationResult(
        entity_type="github",
        action="pr_opened",
        payload={
            "repo": repo,
            "pr_number": pr_number if pr_number is not None else 0,
            "sender": "joe",
            "title": "feat: add the thing",
            "head_branch": "task/x",
            "head_sha": "deadbeef" * 5,
        },
        repo=repo,
        pr_number=pr_number,
    )


_BODY_JSON: dict[str, Any] = {
    "action": "opened",
    "pull_request": {
        "number": 42,
        "title": "feat: add the thing",
        "head": {"ref": "task/x", "sha": "deadbeef" * 5},
        "merged": False,
    },
    "repository": {"full_name": "joe/treadmill"},
    "sender": {"login": "joe"},
}


def _make_session(
    *,
    task_id: uuid.UUID | None,
    event_id_for_get: uuid.UUID | None = None,
) -> Any:
    """Build a stub session that returns ``task_id`` for the task_prs SELECT.

    When ``event_id_for_get`` is set, ``session.get(Event, event_id)`` is
    rigged to return a MagicMock carrying ``id=event_id`` (the SQS-path
    upsert flow re-fetches the row before publish).
    """
    session = MagicMock()
    lookup_result = MagicMock()
    lookup_result.scalar_one_or_none = MagicMock(return_value=task_id)
    insert_result = MagicMock()
    session.execute = AsyncMock(side_effect=[lookup_result, insert_result])
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()

    if event_id_for_get is not None:
        fetched = MagicMock()
        fetched.id = event_id_for_get
        fetched.task_id = task_id
        fetched.entity_type = "github"
        fetched.action = "pr_opened"
        session.get = AsyncMock(return_value=fetched)
    else:
        session.get = AsyncMock(return_value=None)

    return session


# ── HTTP-path (fresh UUID) flow ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_prs_hit_persists_with_task_id_no_buffer() -> None:
    """When the task_prs SELECT resolves, the Event row stamps that
    task_id and the cache-then-heal buffer is NOT touched."""
    resolved = uuid.uuid4()
    session = _make_session(task_id=resolved)
    redis = _StubRedis()
    publisher = _StubPublisher()
    normalized = _normalized_pr_opened()

    event = await persist_and_resolve_webhook_event(
        session, normalized, _BODY_JSON, redis, publisher,
    )

    # Event row attached + committed + refreshed (HTTP-path INSERT path).
    session.add.assert_called_once()
    session.commit.assert_awaited()
    session.refresh.assert_awaited_with(event)
    # No buffer call — task_prs resolved.
    assert redis.rpush_calls == []
    assert redis.expire_calls == []
    # Publisher fired once with the persisted Event.
    assert len(publisher.published) == 1
    assert publisher.published[0][0] is event
    # task_id stamped from the lookup.
    assert event.task_id == resolved


@pytest.mark.asyncio
async def test_task_prs_miss_persists_with_null_and_buffers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the task_prs SELECT misses, the row persists with
    ``task_id=NULL`` and the event_id lands on the (repo, pr_number)
    buffer key. INFO log at ``treadmill.webhooks.persist`` confirms the
    cache-then-heal path took effect."""
    session = _make_session(task_id=None)
    redis = _StubRedis()
    publisher = _StubPublisher()
    normalized = _normalized_pr_opened(repo="Owner/Repo", pr_number=7)

    with caplog.at_level(logging.INFO, logger="treadmill.webhooks.persist"):
        event = await persist_and_resolve_webhook_event(
            session, normalized, _BODY_JSON, redis, publisher,
        )

    # task_id is None on the persisted row.
    assert event.task_id is None
    # Buffer key is the lowercased PR-bound shape.
    assert len(redis.rpush_calls) == 1
    key, raw = redis.rpush_calls[0]
    assert key == "pr:owner/repo:7:pending_events"
    record = json.loads(raw)
    assert record["event_id"] == str(event.id)
    # The buffer-took-effect INFO log lands on the persist logger.
    persist_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.INFO
        and rec.name == "treadmill.webhooks.persist"
    ]
    assert any("buffered pending event_id=" in line for line in persist_lines)


@pytest.mark.asyncio
async def test_event_without_pr_number_skips_lookup_and_buffer() -> None:
    """A normalized event with no (repo, pr_number) pair skips the
    task_prs lookup AND the buffer call — the FK is unresolvable and
    buffering would orphan the entry."""
    session = MagicMock()
    # Only the Event INSERT happens — no task_prs SELECT.
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()

    redis = _StubRedis()
    publisher = _StubPublisher()

    normalized = NormalizationResult(
        entity_type="github",
        action="check_run_completed",
        payload={
            "repo": "joe/treadmill",
            "pr_number": None,
            "check_name": "tests",
            "conclusion": "success",
            "head_sha": "deadbeef" * 5,
        },
        repo="joe/treadmill",
        pr_number=None,
    )
    body = {
        "action": "completed",
        "check_run": {
            "name": "tests",
            "conclusion": "success",
            "head_sha": "deadbeef" * 5,
            "pull_requests": [],
        },
        "repository": {"full_name": "joe/treadmill"},
    }

    event = await persist_and_resolve_webhook_event(
        session, normalized, body, redis, publisher,
    )

    # No task_prs lookup, no buffer call.
    session.execute.assert_not_awaited()
    assert redis.rpush_calls == []
    # Row still persisted + published.
    session.add.assert_called_once()
    session.commit.assert_awaited()
    assert len(publisher.published) == 1
    assert event.task_id is None


@pytest.mark.asyncio
async def test_task_prs_miss_without_redis_client_does_not_crash() -> None:
    """A miss with ``redis_client=None`` skips the buffer call cleanly.
    Used by narrow tests and log-only deployments."""
    session = _make_session(task_id=None)
    publisher = _StubPublisher()
    normalized = _normalized_pr_opened()

    event = await persist_and_resolve_webhook_event(
        session, normalized, _BODY_JSON, None, publisher,
    )

    # No crash; row persisted; publisher fired.
    session.commit.assert_awaited()
    assert len(publisher.published) == 1
    assert event.task_id is None


# ── SQS-path (deterministic event_id) flow ───────────────────────────────────


@pytest.mark.asyncio
async def test_deterministic_event_id_uses_upsert_and_refetches() -> None:
    """Passing ``event_id`` triggers the upsert path: session.execute
    handles the pg_insert + on_conflict_do_nothing; session.get re-fetches
    the canonical row so re-deliveries see the existing fields, not the
    proposed ones."""
    deterministic_id = uuid.uuid5(uuid.NAMESPACE_OID, "delivery-uuid-xyz")
    resolved = uuid.uuid4()
    session = _make_session(
        task_id=resolved, event_id_for_get=deterministic_id,
    )
    redis = _StubRedis()
    publisher = _StubPublisher()
    normalized = _normalized_pr_opened()

    event = await persist_and_resolve_webhook_event(
        session, normalized, _BODY_JSON, redis, publisher,
        event_id=deterministic_id,
    )

    # Three execute calls: task_prs SELECT + pg_insert(...)
    # .on_conflict_do_nothing + the task 5dd4a32d head_sha writer
    # (pr_opened/pr_synchronize UPDATE task_prs SET head_sha).
    assert session.execute.await_count == 3
    # session.add MUST NOT be called — that's the fresh-UUID branch.
    session.add.assert_not_called()
    session.refresh.assert_not_called()
    # session.get re-fetched the row by deterministic_id.
    session.get.assert_awaited()
    get_args = session.get.await_args
    assert get_args.args[1] == deterministic_id
    # The returned event is the re-fetched row.
    assert event.id == deterministic_id


@pytest.mark.asyncio
async def test_deterministic_path_vanished_row_raises() -> None:
    """An upsert that completes but whose re-fetch returns None is a
    "should never happen" — raise so the SQS path bounces the message
    and the next attempt either finds the row or surfaces the bug."""
    deterministic_id = uuid.uuid5(uuid.NAMESPACE_OID, "missing-row-uuid")
    session = _make_session(task_id=None)
    # The fetched row is None — simulate the vanished-row case.
    session.get = AsyncMock(return_value=None)

    redis = _StubRedis()
    publisher = _StubPublisher()
    normalized = _normalized_pr_opened()

    with pytest.raises(RuntimeError, match="vanished after upsert"):
        await persist_and_resolve_webhook_event(
            session, normalized, _BODY_JSON, redis, publisher,
            event_id=deterministic_id,
        )

    # The publish call did not happen — we bailed before that.
    assert publisher.published == []


# ── Publish + buffer error contracts ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_failure_logs_and_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Publish failure is a log + swallow (the Event row is the source
    of truth; the consumer's rescan picks it up)."""
    session = _make_session(task_id=uuid.uuid4())
    redis = _StubRedis()
    publisher = _StubPublisher(raises=RuntimeError("SNS down"))
    normalized = _normalized_pr_opened()

    with caplog.at_level(logging.ERROR, logger="treadmill.webhooks.persist"):
        event = await persist_and_resolve_webhook_event(
            session, normalized, _BODY_JSON, redis, publisher,
        )

    # Returned the persisted Event successfully.
    assert event is not None
    # Logged the publish failure with the event_id.
    error_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.ERROR
        and rec.name == "treadmill.webhooks.persist"
    ]
    assert any("event publish failed" in line for line in error_lines)


@pytest.mark.asyncio
async def test_buffer_failure_logs_and_continues_to_publish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Redis hiccup during the buffer call must not block the publish
    — the Event row is already persisted; degrading the race-window is
    acceptable, losing the publish is not."""
    session = _make_session(task_id=None)
    redis = _StubRedis(raises_on_rpush=RuntimeError("Redis down"))
    publisher = _StubPublisher()
    normalized = _normalized_pr_opened()

    with caplog.at_level(logging.ERROR, logger="treadmill.webhooks.persist"):
        event = await persist_and_resolve_webhook_event(
            session, normalized, _BODY_JSON, redis, publisher,
        )

    # Publish still happened.
    assert len(publisher.published) == 1
    assert publisher.published[0][0] is event
    # Logged the buffer failure with repo + pr_number.
    error_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.ERROR
        and rec.name == "treadmill.webhooks.persist"
    ]
    assert any(
        "pending-event buffering failed" in line for line in error_lines
    )


# ── Validation drift ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_normalizer_drift_propagates_validation_error() -> None:
    """A normalized payload that fails the typed registry raises
    pydantic ValidationError so the caller picks the status code
    (HTTP route → 500; SQS poller → propagate so the message bounces
    for an operator-driven fix)."""
    from pydantic import ValidationError as PydanticValidationError

    session = _make_session(task_id=None)
    redis = _StubRedis()
    publisher = _StubPublisher()
    # Required ``pr_number`` removed → registry validation fails.
    normalized = NormalizationResult(
        entity_type="github",
        action="pr_opened",
        payload={
            "repo": "joe/treadmill",
            # missing pr_number, sender, title, head_branch, head_sha
        },
        repo="joe/treadmill",
        pr_number=42,
    )

    with pytest.raises(PydanticValidationError):
        await persist_and_resolve_webhook_event(
            session, normalized, _BODY_JSON, redis, publisher,
        )

    # No DB work happened — validation runs first.
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
    assert publisher.published == []
