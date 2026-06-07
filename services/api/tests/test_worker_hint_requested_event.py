"""Unit tests for TaskWorkerHintRequested event (ADR-0081)."""

import pytest
import pydantic

from treadmill_api.events import (
    EVENT_REGISTRY,
    TaskWorkerHintRequested,
    encode_payload,
    parse_payload,
)


def _valid_payload(**overrides):
    base = {
        "reason": "tests_need_scope",
        "context_excerpt": "The test count is wrong",
        "worker_step_id": "step-123",
    }
    base.update(overrides)
    return base


def test_empty_reason_rejected():
    with pytest.raises(pydantic.ValidationError):
        TaskWorkerHintRequested(**_valid_payload(reason=""))


def test_reason_too_long_rejected():
    with pytest.raises(pydantic.ValidationError):
        TaskWorkerHintRequested(**_valid_payload(reason="x" * 101))


def test_empty_context_excerpt_rejected():
    with pytest.raises(pydantic.ValidationError):
        TaskWorkerHintRequested(**_valid_payload(context_excerpt=""))


def test_context_excerpt_too_long_rejected():
    with pytest.raises(pydantic.ValidationError):
        TaskWorkerHintRequested(**_valid_payload(context_excerpt="x" * 501))


def test_empty_worker_step_id_rejected():
    with pytest.raises(pydantic.ValidationError):
        TaskWorkerHintRequested(**_valid_payload(worker_step_id=""))


def test_round_trip_all_fields():
    evt = TaskWorkerHintRequested(**_valid_payload())
    encoded = encode_payload(evt)
    decoded = parse_payload("task", "worker_hint_requested", encoded)
    assert isinstance(decoded, TaskWorkerHintRequested)
    assert decoded.reason == evt.reason
    assert decoded.context_excerpt == evt.context_excerpt
    assert decoded.worker_step_id == evt.worker_step_id


def test_round_trip_max_length_fields():
    evt = TaskWorkerHintRequested(**_valid_payload(
        reason="x" * 100,
        context_excerpt="y" * 500,
    ))
    encoded = encode_payload(evt)
    decoded = parse_payload("task", "worker_hint_requested", encoded)
    assert decoded.reason == "x" * 100
    assert decoded.context_excerpt == "y" * 500


def test_registry_contains_worker_hint_requested():
    assert ("task", "worker_hint_requested") in EVENT_REGISTRY
    assert EVENT_REGISTRY[("task", "worker_hint_requested")] is TaskWorkerHintRequested
