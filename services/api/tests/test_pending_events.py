"""Unit tests for ``treadmill_api.webhooks.pending_events``.

After ADR-0063 Step 2 the module exposes an opaque-key API:

  * ``pr_pending_buffer_key(repo, pr_number)`` derives the PR-bound
    Redis list key.
  * ``buffer_pending_event(redis_client, pending_buffer_key, event_id)``
  * ``drain_pending_events(redis_client, session, publisher,
    pending_buffer_key, task_id)``
  * ``pending_event_count(redis_client, pending_buffer_key)``

These tests pin the helper's output and one positive case per
opaque-key function using a synthetic key shape (``"test:abc:pending_events"``)
to prove the buffer/drain/count mechanics are key-agnostic — a future
caller can keyed buffer by any string.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from treadmill_api.webhooks.pending_events import (
    PENDING_TTL_SECONDS,
    buffer_pending_event,
    drain_pending_events,
    pending_event_count,
    pr_pending_buffer_key,
)


# ── Stubs ─────────────────────────────────────────────────────────────────────


class _StubRedis:
    """In-memory async Redis stub covering the four list ops the module uses.

    rpush + expire + lpop + llen — enough for the buffer/drain/count
    surface. Stored per-key so multiple keys can coexist (the opaque-key
    API explicitly allows callers to pick any key shape they want).
    """

    def __init__(self) -> None:
        self._lists: dict[str, list[bytes]] = {}
        self.rpush_calls: list[tuple[str, Any]] = []
        self.expire_calls: list[tuple[str, int]] = []
        self.lpop_calls: list[str] = []
        self.llen_calls: list[str] = []

    async def rpush(self, key: str, value: Any) -> int:
        self.rpush_calls.append((key, value))
        bucket = self._lists.setdefault(key, [])
        bucket.append(value if isinstance(value, bytes) else value.encode())
        return len(bucket)

    async def expire(self, key: str, seconds: int) -> bool:
        self.expire_calls.append((key, seconds))
        return True

    async def lpop(self, key: str) -> bytes | None:
        self.lpop_calls.append(key)
        bucket = self._lists.get(key)
        if not bucket:
            return None
        return bucket.pop(0)

    async def llen(self, key: str) -> int:
        self.llen_calls.append(key)
        return len(self._lists.get(key, []))


class _StubEvent:
    """Minimal stand-in for the ``Event`` ORM row that the drain reads.

    The drain only touches ``entity_type`` / ``action`` / ``payload`` (to
    rebuild the typed payload for re-publish) plus ``task_id`` (the
    single mutable column it writes). A bare object with those
    attributes is enough.
    """

    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.entity_type = "github"
        self.action = "pr_opened"
        self.task_id: uuid.UUID | None = None
        self.payload = {
            "repo": "owner/repo",
            "pr_number": 7,
            "sender": "alice",
            "title": "x",
            "head_branch": "task/x",
            "head_sha": "deadbeef" * 5,
        }


class _StubPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[Any, Any]] = []

    async def publish(self, event: Any, typed: Any) -> None:
        self.published.append((event, typed))


# ── pr_pending_buffer_key helper ──────────────────────────────────────────────


def test_pr_pending_buffer_key_lowercases_repo() -> None:
    """The helper's contract: ``pr:{repo-lower}:{pr_number}:pending_events``.

    Lowercasing matches the case-insensitive task_prs lookup the
    consumer uses on the same (repo, pr_number) pair, so a webhook
    arriving with ``Owner/Repo`` and a back-fill writing ``owner/repo``
    land on the same key.
    """
    assert (
        pr_pending_buffer_key("Owner/Repo", 42)
        == "pr:owner/repo:42:pending_events"
    )
    # Already-lowercased input is a no-op.
    assert (
        pr_pending_buffer_key("owner/repo", 42)
        == "pr:owner/repo:42:pending_events"
    )


# ── buffer_pending_event (opaque-key API) ────────────────────────────────────


@pytest.mark.asyncio
async def test_buffer_pending_event_pushes_under_opaque_key() -> None:
    """``buffer_pending_event`` rpushes a JSON record under the caller-
    supplied key and arms the TTL on the same key. The key shape is
    opaque — a synthetic non-PR key still works."""
    redis = _StubRedis()
    event_id = uuid.uuid4()
    key = "test:abc:pending_events"

    await buffer_pending_event(redis, key, event_id)

    assert len(redis.rpush_calls) == 1
    pushed_key, pushed_value = redis.rpush_calls[0]
    assert pushed_key == key
    record = json.loads(pushed_value)
    assert record["event_id"] == str(event_id)
    assert "buffered_at" in record

    # TTL armed on the same key the record landed in.
    assert redis.expire_calls == [(key, PENDING_TTL_SECONDS)]


# ── drain_pending_events (opaque-key API) ────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_pending_events_resolves_buffered_event() -> None:
    """End-to-end positive case: a single buffered event_id drains,
    its Event row's ``task_id`` is set, ``session.commit()`` is
    awaited once at the end, and the publisher is invoked with the
    rebuilt typed payload.
    """
    redis = _StubRedis()
    event = _StubEvent()
    key = "test:abc:pending_events"

    # Pre-seed the buffer using the same helper a real caller would.
    await buffer_pending_event(redis, key, event.id)
    # ``buffer_pending_event`` logs an rpush — clear the recording so
    # the drain assertions below only see drain-time activity.
    redis.rpush_calls.clear()
    redis.expire_calls.clear()

    session = AsyncMock()
    session.get = AsyncMock(return_value=event)
    session.flush = AsyncMock()
    session.commit = AsyncMock()

    publisher = _StubPublisher()
    task_id = uuid.uuid4()

    drained = await drain_pending_events(
        redis, session, publisher, key, task_id,
    )

    assert drained == 1
    # The drain wrote the resolved task_id onto the Event row.
    assert event.task_id == task_id
    session.flush.assert_awaited()
    session.commit.assert_awaited_once()
    # The publisher saw the resolved Event + rebuilt typed payload.
    assert len(publisher.published) == 1
    published_event, published_typed = publisher.published[0]
    assert published_event is event
    # The typed payload's ENTITY_TYPE/ACTION class-vars match the row.
    assert published_typed.ENTITY_TYPE == "github"
    assert published_typed.ACTION == "pr_opened"
    # Buffer is empty after the drain.
    assert (await redis.llen(key)) == 0


# ── pending_event_count (opaque-key API) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_event_count_returns_buffer_length() -> None:
    """``pending_event_count`` mirrors ``LLEN`` against the opaque key
    without draining the buffer — used by tests + status surfaces."""
    redis = _StubRedis()
    key = "test:abc:pending_events"

    assert (await pending_event_count(redis, key)) == 0

    await buffer_pending_event(redis, key, uuid.uuid4())
    await buffer_pending_event(redis, key, uuid.uuid4())

    assert (await pending_event_count(redis, key)) == 2
    # Inspection didn't drain the buffer.
    assert (await redis.llen(key)) == 2
