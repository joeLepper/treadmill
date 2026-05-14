"""Integration tests for the coordination consumer + task.auto_merged event.

Tests the end-to-end integration between:
  * The ``TaskAutoMerged`` payload class (events/task.py)
  * Its entry in EVENT_REGISTRY (events/registry.py)
  * The consumer's validation gate (consumer.py)

No live Postgres required — the consumer stubs used here are the same
pattern as test_consumer_unit.py.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import ValidationError

from treadmill_api.coordination.consumer import CoordinationConsumer
from treadmill_api.events import (
    EVENT_REGISTRY,
    TaskAutoMerged,
    encode_payload,
    parse_payload,
)
from treadmill_api.events.registry import UnknownEventTypeError


# ── Helpers ──────────────────────────────────────────────────────────────────


class _StubSession:
    def __init__(self) -> None:
        from unittest.mock import AsyncMock
        self.execute = AsyncMock()
        self.commit = AsyncMock()


def _stub_factory(session: _StubSession) -> Any:
    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    def _make() -> Any:
        return _cm()

    return _make


def _consumer(session: _StubSession) -> CoordinationConsumer:
    return CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(session),  # type: ignore[arg-type]
    )


# ── Registry: task.auto_merged is registered ─────────────────────────────────


def test_task_auto_merged_in_event_registry() -> None:
    """``("task", "auto_merged")`` must be present in EVENT_REGISTRY so
    that the consumer validation gate can parse incoming events."""
    assert ("task", "auto_merged") in EVENT_REGISTRY
    assert EVENT_REGISTRY[("task", "auto_merged")] is TaskAutoMerged


# ── Payload: round-trip encode/parse ─────────────────────────────────────────


def test_task_auto_merged_round_trip() -> None:
    """Encoding then parsing ``TaskAutoMerged`` produces an identical
    object — exercises the JSONB encode/decode contract (ADR-0011)."""
    original = TaskAutoMerged(
        merged_sha="abc123def456" * 3,
        pr_number=42,
        repo="acme/backend",
    )
    encoded = encode_payload(original)
    parsed = parse_payload("task", "auto_merged", encoded)
    assert isinstance(parsed, TaskAutoMerged)
    assert parsed == original


def test_task_auto_merged_parse_payload_validates_required_fields() -> None:
    """``parse_payload`` with an empty dict raises ``ValidationError``
    because all three fields (merged_sha, pr_number, repo) are required."""
    with pytest.raises(ValidationError):
        parse_payload("task", "auto_merged", {})


def test_task_auto_merged_parse_payload_rejects_extra_fields() -> None:
    """The strict ``extra="forbid"`` config on EventPayload rejects
    unknown fields so payload drift fails loudly (ADR-0011)."""
    with pytest.raises(ValidationError):
        parse_payload(
            "task",
            "auto_merged",
            {
                "merged_sha": "deadbeef" * 5,
                "pr_number": 1,
                "repo": "x/y",
                "unexpected": "extra",
            },
        )


def test_task_auto_merged_entity_type_and_action() -> None:
    """The ClassVar markers must match the registry key exactly."""
    assert TaskAutoMerged.ENTITY_TYPE == "task"
    assert TaskAutoMerged.ACTION == "auto_merged"


# ── Consumer validation gate ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consumer_accepts_task_auto_merged_event() -> None:
    """A well-formed ``task.auto_merged`` message passes the validation
    gate in ``CoordinationConsumer.handle()`` without raising."""
    session = _StubSession()
    consumer = _consumer(session)

    # handle() should not raise; the consumer may or may not issue SQL
    # depending on whether it has a handler for this event type.
    await consumer.handle({
        "entity_type": "task",
        "action": "auto_merged",
        "task_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "merged_sha": "cafebabe" * 5,
            "pr_number": 7,
            "repo": "acme/backend",
        },
    })


@pytest.mark.asyncio
async def test_consumer_rejects_task_auto_merged_missing_merged_sha() -> None:
    """A ``task.auto_merged`` message with no ``merged_sha`` fails Pydantic
    validation before any SQL is issued — the validation gate is intact."""
    session = _StubSession()
    consumer = _consumer(session)

    await consumer.handle({
        "entity_type": "task",
        "action": "auto_merged",
        "task_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "pr_number": 7,
            "repo": "acme/backend",
            # merged_sha intentionally omitted
        },
    })
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
