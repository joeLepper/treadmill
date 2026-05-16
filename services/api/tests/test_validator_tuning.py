"""Tests for the ValidatorTuning Pydantic envelope (ADR-0040)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from treadmill_api.events import ValidatorTuning


# ── Helpers ───────────────────────────────────────────────────────────────────

_BASE = dict(
    rule_slug="implementation-conforms-to-diagram",
    check_id="diagram-present",
    evidence="The architect verified all cited spec items at file:line; validator fired defensively.",
)


def _make(**overrides) -> dict:
    return {**_BASE, **overrides}


# ── Well-formed round-trips ────────────────────────────────────────────────────


def test_demote_severity_round_trip():
    vt = ValidatorTuning.model_validate(
        _make(
            action="demote_severity",
            proposed_patch={"from": "blocking", "to": "warning"},
        )
    )
    assert vt.action == "demote_severity"
    assert vt.proposed_patch["to"] == "warning"
    restored = ValidatorTuning.model_validate(vt.model_dump(mode="json"))
    assert restored == vt


def test_demote_severity_to_advisory():
    vt = ValidatorTuning.model_validate(
        _make(
            action="demote_severity",
            proposed_patch={"from": "blocking", "to": "advisory"},
        )
    )
    assert vt.proposed_patch["to"] == "advisory"


def test_narrow_applies_to_round_trip():
    vt = ValidatorTuning.model_validate(
        _make(
            action="narrow_applies_to",
            proposed_patch={
                "remove_globs": ["docs/**"],
                "keep_globs": ["services/**", "workers/**"],
            },
        )
    )
    assert vt.action == "narrow_applies_to"
    assert "docs/**" in vt.proposed_patch["remove_globs"]
    restored = ValidatorTuning.model_validate(vt.model_dump(mode="json"))
    assert restored == vt


def test_narrow_applies_to_empty_globs_allowed():
    """Empty glob lists are valid — the operator might clear all remove_globs."""
    vt = ValidatorTuning.model_validate(
        _make(
            action="narrow_applies_to",
            proposed_patch={"remove_globs": [], "keep_globs": []},
        )
    )
    assert vt.proposed_patch["remove_globs"] == []


def test_refine_prompt_round_trip():
    vt = ValidatorTuning.model_validate(
        _make(
            action="refine_prompt",
            proposed_patch={
                "diff_text": "When DIAGRAM_SOURCE is absent, return advisory not blocking."
            },
        )
    )
    assert vt.action == "refine_prompt"
    assert "DIAGRAM_SOURCE" in vt.proposed_patch["diff_text"]
    restored = ValidatorTuning.model_validate(vt.model_dump(mode="json"))
    assert restored == vt


# ── Missing required fields ───────────────────────────────────────────────────


def test_missing_rule_slug_raises():
    data = _make(action="demote_severity", proposed_patch={"from": "blocking", "to": "warning"})
    del data["rule_slug"]
    with pytest.raises(ValidationError):
        ValidatorTuning.model_validate(data)


def test_missing_check_id_raises():
    data = _make(action="demote_severity", proposed_patch={"from": "blocking", "to": "warning"})
    del data["check_id"]
    with pytest.raises(ValidationError):
        ValidatorTuning.model_validate(data)


def test_missing_action_raises():
    data = _make(proposed_patch={"from": "blocking", "to": "warning"})
    with pytest.raises(ValidationError):
        ValidatorTuning.model_validate(data)


def test_missing_evidence_raises():
    data = _make(action="demote_severity", proposed_patch={"from": "blocking", "to": "warning"})
    del data["evidence"]
    with pytest.raises(ValidationError):
        ValidatorTuning.model_validate(data)


def test_missing_proposed_patch_raises():
    data = _make(action="demote_severity")
    with pytest.raises(ValidationError):
        ValidatorTuning.model_validate(data)


# ── Invalid action literal ────────────────────────────────────────────────────


def test_invalid_action_raises():
    with pytest.raises(ValidationError):
        ValidatorTuning.model_validate(
            _make(action="auto_approve", proposed_patch={})
        )


def test_action_is_case_sensitive():
    with pytest.raises(ValidationError):
        ValidatorTuning.model_validate(
            _make(action="Demote_Severity", proposed_patch={"from": "blocking", "to": "warning"})
        )


# ── proposed_patch shape validation — demote_severity ────────────────────────


def test_demote_severity_missing_from_raises():
    with pytest.raises(ValidationError, match="missing keys"):
        ValidatorTuning.model_validate(
            _make(action="demote_severity", proposed_patch={"to": "warning"})
        )


def test_demote_severity_missing_to_raises():
    with pytest.raises(ValidationError, match="missing keys"):
        ValidatorTuning.model_validate(
            _make(action="demote_severity", proposed_patch={"from": "blocking"})
        )


def test_demote_severity_invalid_from_raises():
    with pytest.raises(ValidationError, match="'from' must be 'blocking'"):
        ValidatorTuning.model_validate(
            _make(
                action="demote_severity",
                proposed_patch={"from": "warning", "to": "advisory"},
            )
        )


def test_demote_severity_invalid_to_raises():
    with pytest.raises(ValidationError, match="'to' must be"):
        ValidatorTuning.model_validate(
            _make(
                action="demote_severity",
                proposed_patch={"from": "blocking", "to": "blocking"},
            )
        )


# ── proposed_patch shape validation — narrow_applies_to ──────────────────────


def test_narrow_applies_to_missing_remove_globs_raises():
    with pytest.raises(ValidationError, match="missing keys"):
        ValidatorTuning.model_validate(
            _make(
                action="narrow_applies_to",
                proposed_patch={"keep_globs": ["services/**"]},
            )
        )


def test_narrow_applies_to_missing_keep_globs_raises():
    with pytest.raises(ValidationError, match="missing keys"):
        ValidatorTuning.model_validate(
            _make(
                action="narrow_applies_to",
                proposed_patch={"remove_globs": ["docs/**"]},
            )
        )


def test_narrow_applies_to_non_list_remove_globs_raises():
    with pytest.raises(ValidationError, match="must be a list"):
        ValidatorTuning.model_validate(
            _make(
                action="narrow_applies_to",
                proposed_patch={"remove_globs": "docs/**", "keep_globs": []},
            )
        )


def test_narrow_applies_to_non_list_keep_globs_raises():
    with pytest.raises(ValidationError, match="must be a list"):
        ValidatorTuning.model_validate(
            _make(
                action="narrow_applies_to",
                proposed_patch={"remove_globs": [], "keep_globs": "services/**"},
            )
        )


# ── proposed_patch shape validation — refine_prompt ──────────────────────────


def test_refine_prompt_missing_diff_text_raises():
    with pytest.raises(ValidationError, match="diff_text"):
        ValidatorTuning.model_validate(
            _make(action="refine_prompt", proposed_patch={})
        )


def test_refine_prompt_non_string_diff_text_raises():
    with pytest.raises(ValidationError, match="must be a string"):
        ValidatorTuning.model_validate(
            _make(action="refine_prompt", proposed_patch={"diff_text": 42})
        )


# ── Extra-field discipline ────────────────────────────────────────────────────


def test_extra_top_level_fields_rejected():
    with pytest.raises(ValidationError):
        ValidatorTuning.model_validate(
            _make(
                action="demote_severity",
                proposed_patch={"from": "blocking", "to": "warning"},
                unexpected="field",
            )
        )


# ── Re-export surface ─────────────────────────────────────────────────────────


def test_importable_from_events_package():
    """ValidatorTuning must be importable from the top-level events package."""
    from treadmill_api.events import ValidatorTuning as VT  # noqa: PLC0415

    assert VT is ValidatorTuning


def test_importable_from_registry_module():
    """ValidatorTuning is re-exported from events.registry for consumer code."""
    from treadmill_api.events.registry import ValidatorTuning as VT  # noqa: PLC0415

    assert VT is ValidatorTuning
