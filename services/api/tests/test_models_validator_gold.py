"""Behavioral tests for ``ValidatorGoldRow`` (ADR-0070 substep 3 task 2).

Exercises the ORM model structurally — no live database required. Verifies
that all six ADR-0070 layers are present, column types and nullability match
the migration, CHECK constraints are named per the ``ck_<table>_<col>``
convention, and the partial unlabeled index is wired correctly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

from treadmill_api.models.validator_gold import (
    ValidatorGoldRow,
    LLM_LABEL_VALUES,
    VERDICT_EMITTED_VALUES,
)
from treadmill_api.models.review_queue import ReviewQueueRowMixin

_TABLE = "validator_gold_rows"


# ── Column-shape assertions ───────────────────────────────────────────────────


def test_provenance_layer_columns():
    cols = ValidatorGoldRow.__table__.columns

    assert isinstance(cols["id"].type, UUID)
    assert cols["id"].primary_key
    assert cols["id"].server_default is not None

    assert isinstance(cols["created_at"].type, TIMESTAMP)
    assert cols["created_at"].type.timezone
    assert cols["created_at"].nullable is False
    assert cols["created_at"].server_default is not None

    for name in ("source_run_id", "source_event_id"):
        assert isinstance(cols[name].type, UUID), f"{name} should be UUID"
        assert cols[name].nullable is True, f"{name} should be nullable"

    assert cols["source_pr_number"].nullable is True
    assert cols["source_pr_number"].type.python_type is int

    assert isinstance(cols["source_url"].type, Text)
    assert cols["source_url"].nullable is True


def test_candidate_content_layer_columns():
    cols = ValidatorGoldRow.__table__.columns

    assert isinstance(cols["source_step_id"].type, UUID)
    assert cols["source_step_id"].nullable is False

    assert cols["verdict_emitted"].type.length == 8
    assert cols["verdict_emitted"].nullable is False

    assert isinstance(cols["script_excerpt"].type, Text)
    assert cols["script_excerpt"].nullable is False

    assert isinstance(cols["artifact_excerpt"].type, Text)
    assert cols["artifact_excerpt"].nullable is False


def test_llm_recommendation_layer_columns():
    cols = ValidatorGoldRow.__table__.columns

    assert cols["llm_label"].type.length == 32
    assert cols["llm_label"].nullable is False

    assert cols["llm_confidence"].type.length == 8
    assert cols["llm_confidence"].nullable is False

    for name in ("llm_rationale", "llm_prompt_version", "llm_model"):
        assert isinstance(cols[name].type, Text), f"{name} should be Text"
        assert cols[name].nullable is False, f"{name} should be NOT NULL"


def test_operator_label_layer_columns():
    cols = ValidatorGoldRow.__table__.columns

    assert cols["label_verdict"].type.length == 32
    assert cols["label_verdict"].nullable is True

    for name in ("label_notes", "label_override_reason"):
        assert isinstance(cols[name].type, Text)
        assert cols[name].nullable is True


def test_labeled_metadata_layer_columns():
    cols = ValidatorGoldRow.__table__.columns

    for name in ("labeled_by", "label_guidelines_version"):
        assert isinstance(cols[name].type, Text)
        assert cols[name].nullable is True

    assert isinstance(cols["labeled_at"].type, TIMESTAMP)
    assert cols["labeled_at"].type.timezone
    assert cols["labeled_at"].nullable is True


def test_outcome_layer_columns():
    cols = ValidatorGoldRow.__table__.columns

    assert cols["outcome_state"].type.length == 16
    assert cols["outcome_state"].nullable is True

    assert isinstance(cols["outcome_pr_merged_at"].type, TIMESTAMP)
    assert cols["outcome_pr_merged_at"].type.timezone
    assert cols["outcome_pr_merged_at"].nullable is True


# ── Constraint assertions ─────────────────────────────────────────────────────


def test_check_constraints_are_present():
    check_names = {
        c.name
        for c in ValidatorGoldRow.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    expected = {
        f"ck_{_TABLE}_verdict_emitted",
        f"ck_{_TABLE}_llm_label",
        f"ck_{_TABLE}_label_verdict",
        f"ck_{_TABLE}_llm_confidence",
        f"ck_{_TABLE}_outcome_state",
    }
    for name in expected:
        assert name in check_names, f"missing CHECK constraint {name!r}"


def test_partial_unlabeled_index():
    indexes = {idx.name: idx for idx in ValidatorGoldRow.__table__.indexes}
    assert f"ix_{_TABLE}_unlabeled" in indexes
    idx = indexes[f"ix_{_TABLE}_unlabeled"]
    where = idx.dialect_options["postgresql"].get("where")
    assert where is not None
    assert str(where) == "label_verdict IS NULL"
    assert [c.name for c in idx.columns] == ["label_verdict"]


def test_plain_indexes_present():
    index_names = {idx.name for idx in ValidatorGoldRow.__table__.indexes}
    assert f"ix_{_TABLE}_created_at" in index_names
    assert f"ix_{_TABLE}_verdict_emitted" in index_names


# ── Enum tuple exports ────────────────────────────────────────────────────────


def test_verdict_emitted_values():
    assert VERDICT_EMITTED_VALUES == ("pass", "fail")


def test_llm_label_values():
    assert LLM_LABEL_VALUES == ("correct-verdict", "wrong-verdict", "unclear")


def test_llm_confidence_inherits_mixin_values():
    assert ValidatorGoldRow.LLM_CONFIDENCE_VALUES == ("high", "medium", "low")


# ── Instantiation tests ───────────────────────────────────────────────────────


def test_fully_populated_row_is_accepted():
    """ORM accepts a row with all fields populated (no DB required)."""
    row = ValidatorGoldRow(
        id=uuid.uuid4(),
        source_step_id=uuid.uuid4(),
        verdict_emitted="pass",
        script_excerpt="Check passed validation.",
        artifact_excerpt="stdout: all checks ok",
        llm_label="correct-verdict",
        llm_confidence="high",
        llm_rationale="The verdict is accurate.",
        llm_prompt_version="v1.0.0",
        llm_model="claude-sonnet-4-6",
        label_verdict="correct-verdict",
        label_notes="Agreed with LLM.",
        label_override_reason=None,
        labeled_by="operator",
        labeled_at=datetime.now(timezone.utc),
        label_guidelines_version="v1",
        outcome_state="merged",
        source_run_id=uuid.uuid4(),
        source_event_id=uuid.uuid4(),
        source_pr_number=42,
        source_url="https://github.com/org/repo/pull/42",
    )
    assert row.source_step_id is not None
    assert row.verdict_emitted == "pass"
    assert row.label_verdict == "correct-verdict"
    assert row.labeled_by == "operator"


def test_label_null_row_is_accepted():
    """ORM accepts a row where all operator-label fields are null."""
    row = ValidatorGoldRow(
        id=uuid.uuid4(),
        source_step_id=uuid.uuid4(),
        verdict_emitted="fail",
        script_excerpt="Check failed.",
        artifact_excerpt="stderr: validation error",
        llm_label="wrong-verdict",
        llm_confidence="medium",
        llm_rationale="The verdict appears incorrect.",
        llm_prompt_version="v1.0.0",
        llm_model="claude-sonnet-4-6",
    )
    assert row.label_verdict is None
    assert row.labeled_by is None
    assert row.labeled_at is None
    assert row.outcome_state is None


def test_row_can_be_added_to_stub_session():
    """Construct a row and 'insert' via a stub session — no DB required."""

    class _StubSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, obj: object) -> None:
            self.added.append(obj)

    row = ValidatorGoldRow(
        id=uuid.uuid4(),
        source_step_id=uuid.uuid4(),
        verdict_emitted="pass",
        script_excerpt="Validation succeeded.",
        artifact_excerpt="stdout: ok",
        llm_label="unclear",
        llm_confidence="low",
        llm_rationale="Difficult to judge.",
        llm_prompt_version="v1.0.0",
        llm_model="claude-sonnet-4-6",
    )
    session = _StubSession()
    session.add(row)

    assert len(session.added) == 1
    assert session.added[0] is row
