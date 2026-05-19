"""Unit tests for TaskRetry event payload."""

import pytest
import pydantic

from treadmill_api.events import (
    EVENT_REGISTRY,
    TaskRetry,
    encode_payload,
    parse_payload,
)


def _valid_payload(**overrides):
    base = {
        "workflow_id": "wf-abc123",
        "reason": "Retrying after transient CI failure",
        "by_operator": "alice",
        "bypassed_cap": False,
        "previous_run_id": "wf-run-xyz",
    }
    base.update(overrides)
    return base


def test_empty_reason_rejected():
    with pytest.raises(pydantic.ValidationError):
        TaskRetry(**_valid_payload(reason=""))


def test_reason_too_long_rejected():
    with pytest.raises(pydantic.ValidationError):
        TaskRetry(**_valid_payload(reason="x" * 501))


def test_round_trip_all_fields():
    evt = TaskRetry(**_valid_payload())
    encoded = encode_payload(evt)
    decoded = parse_payload("task", "retry", encoded)
    assert isinstance(decoded, TaskRetry)
    assert decoded.workflow_id == evt.workflow_id
    assert decoded.reason == evt.reason
    assert decoded.by_operator == evt.by_operator
    assert decoded.bypassed_cap == evt.bypassed_cap
    assert decoded.previous_run_id == evt.previous_run_id


def test_round_trip_no_previous_run_id():
    evt = TaskRetry(**_valid_payload(previous_run_id=None))
    encoded = encode_payload(evt)
    decoded = parse_payload("task", "retry", encoded)
    assert isinstance(decoded, TaskRetry)
    assert decoded.previous_run_id is None


def test_round_trip_bypassed_cap_true():
    evt = TaskRetry(**_valid_payload(bypassed_cap=True))
    encoded = encode_payload(evt)
    decoded = parse_payload("task", "retry", encoded)
    assert decoded.bypassed_cap is True


def test_registry_contains_task_retry():
    assert ("task", "retry") in EVENT_REGISTRY
    assert EVENT_REGISTRY[("task", "retry")] is TaskRetry
