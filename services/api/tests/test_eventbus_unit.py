"""Unit tests for the SNS publisher's typed error handling.

Two failure modes — transport error vs validation error — must be
distinguishable so the dispatcher can persist a
``dispatch_publish_failed`` marker for the former and fail the request
for the latter (per the 2026-05-11 closure plan, decision #3).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from treadmill_api.eventbus import PublishError, SNSEventPublisher
from treadmill_api.events import TaskRegistered
from treadmill_api.models import Event


def _make_event() -> Event:
    """Construct a minimal Event row in-memory (no DB roundtrip)."""
    return Event(
        id=uuid.uuid4(),
        entity_type="task",
        action="registered",
        task_id=uuid.uuid4(),
        plan_id=None,
        run_id=None,
        step_id=None,
        payload={},
        created_at=datetime.now(timezone.utc),
    )


def _valid_payload() -> TaskRegistered:
    return TaskRegistered(
        repo="RAMJAC/treadmill",
        title="Add /health endpoint",
        workflow_version_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_sns_publisher_wraps_client_error_as_publish_error() -> None:
    """When boto3 raises ``ClientError``, the publisher wraps it as
    ``PublishError`` so callers can distinguish transport failures from
    payload-validation failures."""
    sns_client = Mock()
    sns_client.publish.side_effect = ClientError(
        {"Error": {"Code": "InternalError", "Message": "boom"}},
        "Publish",
    )
    publisher = SNSEventPublisher(sns_client, topic_arn="arn:aws:sns:test:000:topic")

    with pytest.raises(PublishError) as exc_info:
        await publisher.publish(_make_event(), _valid_payload())

    # The original ``ClientError`` is chained so diagnosis is possible.
    assert isinstance(exc_info.value.__cause__, ClientError)
    # The message names the event so logs aren't generic.
    assert "task.registered" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sns_publisher_propagates_validation_failure_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``pydantic.ValidationError`` surfaces during payload encoding,
    the publisher does NOT wrap it as ``PublishError`` — that error class
    is reserved for transport (boto3) failures. A validation error means
    the typed payload is malformed and the request should fail loudly
    rather than be retried by the replay loop."""
    sns_client = Mock()
    publisher = SNSEventPublisher(sns_client, topic_arn="arn:aws:sns:test:000:topic")

    # Simulate encode_payload raising a ValidationError — what would happen
    # if someone tried to publish a typed payload that fails its schema.
    # Use a fresh ValidationError raised from a real model so the shape is
    # authentic.
    def _broken_encode(_payload: object) -> dict[str, object]:
        TaskRegistered.model_validate(
            {"repo": "x", "title": "y"}  # missing workflow_version_id + plan_id
        )
        return {}

    monkeypatch.setattr("treadmill_api.eventbus.encode_payload", _broken_encode)

    with pytest.raises(ValidationError):
        await publisher.publish(_make_event(), _valid_payload())

    # And the SNS client was never called — we failed before transport.
    sns_client.publish.assert_not_called()
