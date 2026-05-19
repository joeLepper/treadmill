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
    """A verdict with remediation_summary validates. Per ADR-0049
    supersede now requires ``rewritten_description``; amend uses
    ``remediation_summary``."""
    verdict = ArchitectVerdict.model_validate({
        "verdict": "amend",
        "reasoning": "The intent applies but the code drifted",
        "target_artifact": "docs/adrs/0025-old-pattern.md",
        "remediation_summary": "Refactor to use new pattern from ADR-0032",
    })
    assert verdict.verdict == "amend"
    assert verdict.remediation_summary == "Refactor to use new pattern from ADR-0032"


def test_architect_verdict_all_verdict_values() -> None:
    """All three verdict values are accepted. Per ADR-0049 supersede
    requires ``rewritten_description``; we supply one here so the
    validator passes."""
    for verdict_value in ["amend", "accept-as-is"]:
        verdict = ArchitectVerdict.model_validate({
            "verdict": verdict_value,
            "reasoning": "Test reasoning",
            "target_artifact": "docs/test.md",
        })
        assert verdict.verdict == verdict_value
    # supersede requires the rewritten_description (ADR-0049).
    supersede = ArchitectVerdict.model_validate({
        "verdict": "supersede",
        "reasoning": "Plan text was wrong",
        "target_artifact": "docs/test.md",
        "rewritten_description": "Corrected: write X to Y.",
    })
    assert supersede.verdict == "supersede"
    assert supersede.rewritten_description == "Corrected: write X to Y."


def test_architect_verdict_rejects_invalid_verdict() -> None:
    """Closed value-set rejects anything outside the three verdicts."""
    with pytest.raises(ValidationError):
        ArchitectVerdict.model_validate({
            "verdict": "lgtm",
            "reasoning": "Invalid verdict",
            "target_artifact": "docs/test.md",
        })


def test_architect_verdict_rejects_uncertain() -> None:
    """Per ADR-0049, ``uncertain`` was removed from the verdict surface;
    the architect must always commit to one of the three actionable
    verdicts. Pydantic rejects the old value."""
    with pytest.raises(ValidationError):
        ArchitectVerdict.model_validate({
            "verdict": "uncertain",
            "reasoning": "Need more context",
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


# ── ADR-0049: supersede requires rewritten_description ──────────────────────


def test_architect_verdict_supersede_requires_rewritten_description() -> None:
    """Per ADR-0049, ``verdict='supersede'`` must carry a non-empty
    ``rewritten_description``. Without it the supersede trigger has no
    text to put on the child task row, so a missing-field supersede is
    a parse failure. The model_validator surfaces the error at validate
    time."""
    with pytest.raises(ValidationError) as exc_info:
        ArchitectVerdict.model_validate({
            "verdict": "supersede",
            "reasoning": "The plan was wrong",
            "target_artifact": "docs/plans/x.md",
        })
    assert "rewritten_description" in str(exc_info.value)


def test_architect_verdict_supersede_rejects_empty_rewritten_description() -> None:
    """Empty / whitespace-only ``rewritten_description`` fails the
    supersede validator (same gate as missing — empty rewrite has no
    child-task content)."""
    for empty in ("", "   ", "\n\t"):
        with pytest.raises(ValidationError):
            ArchitectVerdict.model_validate({
                "verdict": "supersede",
                "reasoning": "The plan was wrong",
                "target_artifact": "docs/plans/x.md",
                "rewritten_description": empty,
            })


def test_architect_verdict_supersede_accepts_non_empty_rewritten_description() -> None:
    """A supersede with a substantive ``rewritten_description`` passes."""
    verdict = ArchitectVerdict.model_validate({
        "verdict": "supersede",
        "reasoning": "Plan text named the wrong file paths",
        "target_artifact": "docs/plans/x.md",
        "rewritten_description": (
            "Write services/api/treadmill_api/foo.py with function "
            "bar() that returns the new shape."
        ),
    })
    assert verdict.verdict == "supersede"
    assert "services/api" in verdict.rewritten_description


def test_architect_verdict_amend_does_not_require_rewritten_description() -> None:
    """``amend`` does NOT require ``rewritten_description`` — the
    validator only gates on supersede. Amend remains the
    ``remediation_summary``-based verdict."""
    verdict = ArchitectVerdict.model_validate({
        "verdict": "amend",
        "reasoning": "The code drifted from the plan",
        "target_artifact": "services/api/x.py",
        "remediation_summary": "Wrap call in idempotency guard",
    })
    assert verdict.rewritten_description is None


def test_architect_verdict_accept_as_is_does_not_require_rewritten_description() -> None:
    """``accept-as-is`` does NOT require ``rewritten_description`` —
    the validator only gates on supersede."""
    verdict = ArchitectVerdict.model_validate({
        "verdict": "accept-as-is",
        "reasoning": "Gap is acceptable",
        "target_artifact": "workers/agent/AGENT.md",
    })
    assert verdict.rewritten_description is None
