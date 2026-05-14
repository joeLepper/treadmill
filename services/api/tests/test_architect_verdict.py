"""Tests for ArchitectVerdict Pydantic model (ADR-0032)."""

import pytest
from pydantic import ValidationError

from treadmill_api.events import ArchitectVerdict


def test_architect_verdict_well_formed() -> None:
    """A well-formed verdict with all required fields validates."""
    verdict = ArchitectVerdict.model_validate({
        "verdict": "amend",
        "reasoning": "The code violates DRY principle",
        "target_artifact": "docs/adrs/0030-federated-context.md",
    })
    assert verdict.verdict == "amend"
    assert verdict.reasoning == "The code violates DRY principle"
    assert verdict.target_artifact == "docs/adrs/0030-federated-context.md"
    assert verdict.remediation_summary is None


def test_architect_verdict_with_remediation_summary() -> None:
    """A verdict with remediation_summary validates."""
    verdict = ArchitectVerdict.model_validate({
        "verdict": "supersede",
        "reasoning": "The intent no longer applies",
        "target_artifact": "docs/adrs/0025-old-pattern.md",
        "remediation_summary": "Refactor to use new pattern from ADR-0032",
    })
    assert verdict.verdict == "supersede"
    assert verdict.remediation_summary == "Refactor to use new pattern from ADR-0032"


def test_architect_verdict_all_verdict_values() -> None:
    """All four verdict values are accepted."""
    for verdict_value in ["amend", "supersede", "accept-as-is", "uncertain"]:
        verdict = ArchitectVerdict.model_validate({
            "verdict": verdict_value,
            "reasoning": "Test reasoning",
            "target_artifact": "docs/test.md",
        })
        assert verdict.verdict == verdict_value


def test_architect_verdict_rejects_invalid_verdict() -> None:
    """Closed value-set rejects anything outside the four verdicts."""
    with pytest.raises(ValidationError):
        ArchitectVerdict.model_validate({
            "verdict": "lgtm",
            "reasoning": "Invalid verdict",
            "target_artifact": "docs/test.md",
        })


def test_architect_verdict_rejects_missing_verdict() -> None:
    """Missing verdict field raises ValidationError."""
    with pytest.raises(ValidationError):
        ArchitectVerdict.model_validate({
            "reasoning": "Test reasoning",
            "target_artifact": "docs/test.md",
        })


def test_architect_verdict_rejects_missing_reasoning() -> None:
    """Missing reasoning field raises ValidationError."""
    with pytest.raises(ValidationError):
        ArchitectVerdict.model_validate({
            "verdict": "amend",
            "target_artifact": "docs/test.md",
        })


def test_architect_verdict_rejects_missing_target_artifact() -> None:
    """Missing target_artifact field raises ValidationError."""
    with pytest.raises(ValidationError):
        ArchitectVerdict.model_validate({
            "verdict": "amend",
            "reasoning": "Test reasoning",
        })


def test_architect_verdict_rejects_unknown_fields() -> None:
    """Extra fields are rejected per extra='forbid' config."""
    with pytest.raises(ValidationError):
        ArchitectVerdict.model_validate({
            "verdict": "amend",
            "reasoning": "Test reasoning",
            "target_artifact": "docs/test.md",
            "unknown_field": "should fail",
        })
