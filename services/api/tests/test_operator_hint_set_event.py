"""Unit tests for OperatorHintSet event (ADR-0081)."""

import pytest
import pydantic

from treadmill_api.events import (
    EVENT_REGISTRY,
    OperatorHintSet,
    encode_payload,
    parse_payload,
)


def _valid_payload(**overrides):
    base = {
        "note_excerpt": "This is the operator hint text",
        "set_by": "operator",
    }
    base.update(overrides)
    return base


def test_empty_excerpt_rejected():
    with pytest.raises(pydantic.ValidationError):
        OperatorHintSet(**_valid_payload(note_excerpt=""))


def test_excerpt_too_long_rejected():
    with pytest.raises(pydantic.ValidationError):
        OperatorHintSet(**_valid_payload(note_excerpt="x" * 501))


def test_empty_set_by_rejected():
    with pytest.raises(pydantic.ValidationError):
        OperatorHintSet(**_valid_payload(set_by=""))


def test_round_trip_all_fields():
    evt = OperatorHintSet(**_valid_payload())
    encoded = encode_payload(evt)
    decoded = parse_payload("task", "operator_hint_set", encoded)
    assert isinstance(decoded, OperatorHintSet)
    assert decoded.note_excerpt == evt.note_excerpt
    assert decoded.set_by == evt.set_by


def test_round_trip_cleared_note():
    evt = OperatorHintSet(**_valid_payload(note_excerpt="(cleared)"))
    encoded = encode_payload(evt)
    decoded = parse_payload("task", "operator_hint_set", encoded)
    assert decoded.note_excerpt == "(cleared)"


def test_registry_contains_operator_hint_set():
    assert ("task", "operator_hint_set") in EVENT_REGISTRY
    assert EVENT_REGISTRY[("task", "operator_hint_set")] is OperatorHintSet
