"""Unit tests for the ``TaskWorkerDepsFailed`` event payload (ADR-0059 Step 4).

The runner emits this event alongside ``step.failed`` when
``repo_deps.materialize`` raises ``WorkerDepsMaterializationError``;
these tests pin the wire shape so a future field rename or relaxed
validator doesn't silently change what the operator dashboard sees.
"""

import uuid

import pydantic
import pytest

from treadmill_api.events import (
    EVENT_REGISTRY,
    TaskWorkerDepsFailed,
    encode_payload,
    parse_payload,
)


def _valid_payload(**overrides):
    base = {
        "task_id": uuid.uuid4(),
        "repo": "joeLepper/treadmill",
        "stage": "python",
        "detail": "pip install failed: No matching distribution found for aws-cdk-lib==99.99.99",
        "worker_deps_hash": "a" * 64,
    }
    base.update(overrides)
    return base


def test_payload_validates_with_all_required_fields():
    evt = TaskWorkerDepsFailed(**_valid_payload())
    assert evt.repo == "joeLepper/treadmill"
    assert evt.stage == "python"
    assert evt.detail.startswith("pip install failed:")
    assert evt.worker_deps_hash == "a" * 64


def test_invalid_stage_rejected():
    with pytest.raises(pydantic.ValidationError):
        TaskWorkerDepsFailed(**_valid_payload(stage="rust"))


def test_empty_detail_rejected():
    with pytest.raises(pydantic.ValidationError):
        TaskWorkerDepsFailed(**_valid_payload(detail=""))


def test_round_trip_model_dump_json():
    evt = TaskWorkerDepsFailed(**_valid_payload(stage="binary"))
    restored = TaskWorkerDepsFailed.model_validate_json(evt.model_dump_json())
    assert restored.task_id == evt.task_id
    assert restored.repo == evt.repo
    assert restored.stage == "binary"
    assert restored.detail == evt.detail
    assert restored.worker_deps_hash == evt.worker_deps_hash


def test_round_trip_through_encode_parse():
    evt = TaskWorkerDepsFailed(**_valid_payload(stage="node"))
    encoded = encode_payload(evt)
    decoded = parse_payload("task", "worker_deps_failed", encoded)
    assert isinstance(decoded, TaskWorkerDepsFailed)
    assert decoded.stage == "node"
    assert decoded.task_id == evt.task_id


def test_registry_contains_worker_deps_failed():
    assert ("task", "worker_deps_failed") in EVENT_REGISTRY
    assert EVENT_REGISTRY[("task", "worker_deps_failed")] is TaskWorkerDepsFailed
