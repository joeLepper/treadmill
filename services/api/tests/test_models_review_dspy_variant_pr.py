"""Non-DB structural tests for the ADR-0070 review_dspy_variant_pr model + schema.

These tests run in the worker sandbox (no Postgres, no
``TREADMILL_INTEGRATION=1`` flag). The Postgres round-trip cases live in
``test_models_review_dspy_variant_pr_integration.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from treadmill_api.models.review_dspy_variant_pr import ReviewDspyVariantPrRow
from treadmill_api.schemas.review_dspy_variant_pr import (
    LabelDspyVariantPrRequest,
    ReviewDspyVariantPr,
)


_EXPECTED_CHECK_NAMES = {
    "ck_review_dspy_variant_pr_llm_label",
    "ck_review_dspy_variant_pr_llm_confidence",
    "ck_review_dspy_variant_pr_label_verdict",
    "ck_review_dspy_variant_pr_outcome_state",
}


def test_table_name() -> None:
    assert ReviewDspyVariantPrRow.__tablename__ == "review_dspy_variant_pr"


def test_check_constraint_names_present() -> None:
    """The four named CHECK constraints must be declared on ``__table__``."""
    present = {
        c.name
        for c in ReviewDspyVariantPrRow.__table__.constraints
        if c.name is not None
    }
    missing = _EXPECTED_CHECK_NAMES - present
    assert not missing, f"missing CHECK constraints: {sorted(missing)}"


def test_partial_index_present() -> None:
    """The partial index must filter on ``label_verdict IS NULL``."""
    indexes = {idx.name: idx for idx in ReviewDspyVariantPrRow.__table__.indexes}
    idx = indexes.get("ix_review_dspy_variant_pr_unlabeled")
    assert idx is not None, "partial index ix_review_dspy_variant_pr_unlabeled missing"
    where_clause = idx.dialect_kwargs.get("postgresql_where")
    assert where_clause is not None, "postgresql_where missing from partial index"
    compiled = str(where_clause)
    assert "label_verdict IS NULL" in compiled, (
        f"unexpected partial-index predicate: {compiled!r}"
    )


def _base_row_attrs(**overrides: object) -> SimpleNamespace:
    """Minimal SimpleNamespace mirroring ORM attrs; overrides replace fields."""
    base: dict = {
        "id": uuid.uuid4(),
        "created_at": datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc),
        "source_run_id": uuid.uuid4(),
        "source_pr_number": 1234,
        "source_pr_url": "https://github.com/joeLepper/treadmill/pull/1234",
        "judge_role": "role-architect",
        "judge_prompt_path": "treadmill_api/starters/role_architect.md",
        "current_score": 0.7000,
        "variant_score": 0.7543,
        "improvement": 0.0543,
        "patch_diff": "--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-old\n+new\n",
        "corpus_s3_uri": "s3://treadmill-personal/optimizer/runs/x/corpus.jsonl",
        "llm_label": "merge",
        "llm_confidence": "high",
        "llm_rationale": "Higher score, no regressions in spot checks.",
        "llm_prompt_version": "v1.0.0",
        "llm_model": "claude-opus-4-7",
        "label_verdict": None,
        "label_notes": None,
        "label_override_reason": None,
        "labeled_by": None,
        "labeled_at": None,
        "label_guidelines_version": None,
        "outcome_state": None,
        "outcome_merged_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_pydantic_schema_round_trip() -> None:
    """Pydantic accepts a row-shaped namespace and rejects extras + bad enums."""
    attrs = _base_row_attrs()
    parsed = ReviewDspyVariantPr.model_validate(attrs, from_attributes=True)
    assert parsed.id == attrs.id
    assert parsed.llm_label == "merge"
    assert parsed.label_verdict is None

    with pytest.raises(ValidationError):
        ReviewDspyVariantPr.model_validate(
            _base_row_attrs(llm_label="bogus"),
            from_attributes=True,
        )


def test_pydantic_score_json_round_trip() -> None:
    """Scores serialize as JSON numbers (not strings) and round-trip cleanly."""
    parsed = ReviewDspyVariantPr.model_validate(
        _base_row_attrs(current_score=0.7543),
        from_attributes=True,
    )
    dumped = parsed.model_dump_json()
    assert "0.7543" in dumped
    assert '"0.7543"' not in dumped

    reparsed = ReviewDspyVariantPr.model_validate_json(dumped)
    assert reparsed.current_score == parsed.current_score


def test_label_request_requires_labeled_by() -> None:
    with pytest.raises(ValidationError):
        LabelDspyVariantPrRequest(label_verdict="merge")  # type: ignore[call-arg]


def test_label_request_override_reason_non_empty_when_supplied() -> None:
    with pytest.raises(ValidationError):
        LabelDspyVariantPrRequest(
            label_verdict="merge",
            label_override_reason="",
            labeled_by="operator",
        )
