"""Unit tests for ArchitectEmitFailure event payload (ADR-0083)."""

import pytest
import pydantic

from treadmill_api.events import (
    EVENT_REGISTRY,
    ArchitectEmitFailure,
    encode_payload,
    parse_payload,
)


def _valid_payload(**overrides):
    base = {
        "parse_failure_reason": "no-structured-output",
        "model_output_excerpt": "The architect output was truncated mid-sentence...",
        "created_by": "treadmill-bert",
        "failing_run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    }
    base.update(overrides)
    return base


def test_valid_payload_constructs():
    evt = ArchitectEmitFailure(**_valid_payload())
    assert evt.parse_failure_reason == "no-structured-output"
    assert evt.created_by == "treadmill-bert"


@pytest.mark.parametrize("reason", [
    "no-structured-output",
    "supersede-missing-rewrite",
    "gate-broken-missing-excerpt",
    "invalid-verdict-literal",
])
def test_all_valid_parse_failure_reasons(reason):
    evt = ArchitectEmitFailure(**_valid_payload(parse_failure_reason=reason))
    assert evt.parse_failure_reason == reason


def test_invalid_parse_failure_reason_rejected():
    with pytest.raises(pydantic.ValidationError):
        ArchitectEmitFailure(**_valid_payload(parse_failure_reason="made-up-reason"))


def test_empty_created_by_rejected():
    with pytest.raises(pydantic.ValidationError):
        ArchitectEmitFailure(**_valid_payload(created_by=""))


def test_empty_failing_run_id_rejected():
    with pytest.raises(pydantic.ValidationError):
        ArchitectEmitFailure(**_valid_payload(failing_run_id=""))


def test_model_output_excerpt_max_length_accepted():
    ArchitectEmitFailure(**_valid_payload(model_output_excerpt="x" * 4096))


def test_model_output_excerpt_too_long_rejected():
    with pytest.raises(pydantic.ValidationError):
        ArchitectEmitFailure(**_valid_payload(model_output_excerpt="x" * 4097))


def test_empty_model_output_excerpt_accepted():
    evt = ArchitectEmitFailure(**_valid_payload(model_output_excerpt=""))
    assert evt.model_output_excerpt == ""


def test_round_trip_encode_decode():
    evt = ArchitectEmitFailure(**_valid_payload())
    encoded = encode_payload(evt)
    decoded = parse_payload("task", "architect_emit_failure", encoded)
    assert isinstance(decoded, ArchitectEmitFailure)
    assert decoded.parse_failure_reason == evt.parse_failure_reason
    assert decoded.created_by == evt.created_by
    assert decoded.failing_run_id == evt.failing_run_id
    assert decoded.model_output_excerpt == evt.model_output_excerpt


def test_registry_contains_architect_emit_failure():
    assert ("task", "architect_emit_failure") in EVENT_REGISTRY
    assert EVENT_REGISTRY[("task", "architect_emit_failure")] is ArchitectEmitFailure
