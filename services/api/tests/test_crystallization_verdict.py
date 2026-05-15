"""Tests for CrystallizationVerdict Pydantic model (ADR-0032)."""

import pytest
from pydantic import ValidationError

from treadmill_api.events import CrystallizationVerdict


def test_crystallization_verdict_well_formed() -> None:
    """A well-formed verdict with all required fields validates."""
    verdict = CrystallizationVerdict.model_validate({
        "verdict": "ready",
        "reasoning": "The learning is stable and ready to formalize",
        "learning_slug": "pattern-singleton-inject",
    })
    assert verdict.verdict == "ready"
    assert verdict.reasoning == "The learning is stable and ready to formalize"
    assert verdict.learning_slug == "pattern-singleton-inject"
    assert verdict.proposed_rule_slug is None


def test_crystallization_verdict_with_proposed_rule_slug() -> None:
    """A verdict with proposed_rule_slug validates."""
    verdict = CrystallizationVerdict.model_validate({
        "verdict": "ready",
        "reasoning": "The learning is stable and ready to formalize",
        "learning_slug": "pattern-singleton-inject",
        "proposed_rule_slug": "rule-0045-singleton-injection",
    })
    assert verdict.verdict == "ready"
    assert verdict.proposed_rule_slug == "rule-0045-singleton-injection"


def test_crystallization_verdict_all_verdict_values() -> None:
    """All three verdict values are accepted."""
    for verdict_value in ["ready", "not-ready", "defer"]:
        verdict = CrystallizationVerdict.model_validate({
            "verdict": verdict_value,
            "reasoning": "Test reasoning",
            "learning_slug": "test-learning",
        })
        assert verdict.verdict == verdict_value


def test_crystallization_verdict_rejects_invalid_verdict() -> None:
    """Closed value-set rejects anything outside the three verdicts."""
    with pytest.raises(ValidationError):
        CrystallizationVerdict.model_validate({
            "verdict": "uncertain",
            "reasoning": "Invalid verdict",
            "learning_slug": "test-learning",
        })


def test_crystallization_verdict_rejects_missing_verdict() -> None:
    """Missing verdict field raises ValidationError."""
    with pytest.raises(ValidationError):
        CrystallizationVerdict.model_validate({
            "reasoning": "Test reasoning",
            "learning_slug": "test-learning",
        })


def test_crystallization_verdict_rejects_missing_reasoning() -> None:
    """Missing reasoning field raises ValidationError."""
    with pytest.raises(ValidationError):
        CrystallizationVerdict.model_validate({
            "verdict": "ready",
            "learning_slug": "test-learning",
        })


def test_crystallization_verdict_rejects_missing_learning_slug() -> None:
    """Missing learning_slug field raises ValidationError."""
    with pytest.raises(ValidationError):
        CrystallizationVerdict.model_validate({
            "verdict": "ready",
            "reasoning": "Test reasoning",
        })


def test_crystallization_verdict_rejects_unknown_fields() -> None:
    """Extra fields are rejected per extra='forbid' config."""
    with pytest.raises(ValidationError):
        CrystallizationVerdict.model_validate({
            "verdict": "ready",
            "reasoning": "Test reasoning",
            "learning_slug": "test-learning",
            "unknown_field": "should fail",
        })
