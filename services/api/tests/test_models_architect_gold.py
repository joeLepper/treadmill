"""Behavioral tests for ``ArchitectGoldRow`` (ADR-0070 substep 3 task 1).

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

from treadmill_api.models.architect_gold import (
    ArchitectGoldRow,
    LLM_LABEL_VALUES,
    VERDICT_EMITTED_VALUES,
)
from treadmill_api.models.review_queue import ReviewQueueRowMixin

_TABLE = "architect_gold_rows"


# ── Column-shape assertions ───────────────────────────────────────────────────


def test_provenance_layer_columns():
    cols = ArchitectGoldRow.__table__.columns

    assert isinstance(cols["id"].type, UUID)
    assert cols["id"].primary_key
    assert cols["id"].server_default is not None

    assert isinstance(cols["created_at"].type, TIMESTAMP)
    assert cols["created_at"].type.timezone
    assert cols["created_at"].nullable is False
    assert cols["created_at"].server_default is not None

    for name in ("source_run_id", "source_event_id", "source_task_id"):
        assert isinstance(cols[name].type, UUID), f"{name} should be UUID"
        assert cols[name].nullable is True, f"{name} should be nullable"

    assert cols["source_pr_number"].nullable is True
    assert cols["source_pr_number"].type.python_type is int

    assert isinstance(cols["source_url"].type, Text)
    assert cols["source_url"].nullable is True


def test_candidate_content_layer_columns():
    cols = ArchitectGoldRow.__table__.columns

    assert isinstance(cols["decision_id"].type, Text)
    assert cols["decision_id"].nullable is False

    assert cols["verdict_emitted"].type.length == 32
    assert cols["verdict_emitted"].nullable is False

    assert isinstance(cols["rationale_excerpt"].type, Text)
    assert cols["rationale_excerpt"].nullable is False

    assert isinstance(cols["gate_log_uri"].type, Text)
    assert cols["gate_log_uri"].nullable is True


def test_llm_recommendation_layer_columns():
    cols = ArchitectGoldRow.__table__.columns

    assert cols["llm_label"].type.length == 32
    assert cols["llm_label"].nullable is False

    assert cols["llm_confidence"].type.length == 8
    assert cols["llm_confidence"].nullable is False

    for name in ("llm_rationale", "llm_prompt_version", "llm_model"):
        assert isinstance(cols[name].type, Text), f"{name} should be Text"
        assert cols[name].nullable is False, f"{name} should be NOT NULL"


def test_operator_label_layer_columns():
    cols = ArchitectGoldRow.__table__.columns

    assert cols["label_verdict"].type.length == 32
    assert cols["label_verdict"].nullable is True

    for name in ("label_notes", "label_override_reason"):
        assert isinstance(cols[name].type, Text)
        assert cols[name].nullable is True


def test_labeled_metadata_layer_columns():
    cols = ArchitectGoldRow.__table__.columns

    for name in ("labeled_by", "label_guidelines_version"):
        assert isinstance(cols[name].type, Text)
        assert cols[name].nullable is True

    assert isinstance(cols["labeled_at"].type, TIMESTAMP)
    assert cols["labeled_at"].type.timezone
    assert cols["labeled_at"].nullable is True


def test_outcome_layer_columns():
    cols = ArchitectGoldRow.__table__.columns

    assert cols["outcome_state"].type.length == 16
    assert cols["outcome_state"].nullable is True

    assert isinstance(cols["outcome_pr_merged_at"].type, TIMESTAMP)
    assert cols["outcome_pr_merged_at"].type.timezone
    assert cols["outcome_pr_merged_at"].nullable is True


# ── Constraint assertions ─────────────────────────────────────────────────────


def test_check_constraints_are_present():
    check_names = {
        c.name
        for c in ArchitectGoldRow.__table__.constraints
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
    indexes = {idx.name: idx for idx in ArchitectGoldRow.__table__.indexes}
    assert f"ix_{_TABLE}_unlabeled" in indexes
    idx = indexes[f"ix_{_TABLE}_unlabeled"]
    where = idx.dialect_options["postgresql"].get("where")
    assert where is not None
    assert str(where) == "label_verdict IS NULL"
    assert [c.name for c in idx.columns] == ["label_verdict"]


def test_plain_indexes_present():
    index_names = {idx.name for idx in ArchitectGoldRow.__table__.indexes}
    assert f"ix_{_TABLE}_created_at" in index_names
    assert f"ix_{_TABLE}_verdict_emitted" in index_names


# ── Enum tuple exports ────────────────────────────────────────────────────────


def test_verdict_emitted_values():
    assert VERDICT_EMITTED_VALUES == ("accept-as-is", "amend", "gate-broken")


def test_llm_label_values():
    assert LLM_LABEL_VALUES == ("too-permissive", "too-strict", "correct", "exclude")


def test_llm_confidence_inherits_mixin_values():
    assert ArchitectGoldRow.LLM_CONFIDENCE_VALUES == ("high", "medium", "low")


# ── Instantiation tests ───────────────────────────────────────────────────────


def test_fully_populated_row_is_accepted():
    """ORM accepts a row with all fields populated (no DB required)."""
    row = ArchitectGoldRow(
        id=uuid.uuid4(),
        decision_id="plan/abc123",
        verdict_emitted="accept-as-is",
        rationale_excerpt="The change looks good.",
        gate_log_uri="s3://bucket/gate.log",
        llm_label="correct",
        llm_confidence="high",
        llm_rationale="The verdict matches best practices.",
        llm_prompt_version="v1.0.0",
        llm_model="claude-sonnet-4-6",
        label_verdict="correct",
        label_notes="Agreed with LLM.",
        label_override_reason=None,
        labeled_by="operator",
        labeled_at=datetime.now(timezone.utc),
        label_guidelines_version="v1",
        outcome_state="merged",
        source_run_id=uuid.uuid4(),
        source_event_id=uuid.uuid4(),
        source_task_id=uuid.uuid4(),
        source_pr_number=42,
        source_url="https://github.com/org/repo/pull/42",
    )
    assert row.decision_id == "plan/abc123"
    assert row.verdict_emitted == "accept-as-is"
    assert row.label_verdict == "correct"
    assert row.labeled_by == "operator"


def test_label_null_row_is_accepted():
    """ORM accepts a row where all operator-label fields are null."""
    row = ArchitectGoldRow(
        id=uuid.uuid4(),
        decision_id="plan/xyz789",
        verdict_emitted="amend",
        rationale_excerpt="Minor style issue.",
        llm_label="too-strict",
        llm_confidence="medium",
        llm_rationale="The change is over-restrictive.",
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

    row = ArchitectGoldRow(
        id=uuid.uuid4(),
        decision_id="plan/stub",
        verdict_emitted="gate-broken",
        rationale_excerpt="Gate failed.",
        llm_label="exclude",
        llm_confidence="low",
        llm_rationale="Tooling failure, not a real verdict.",
        llm_prompt_version="v1.0.0",
        llm_model="claude-sonnet-4-6",
    )
    session = _StubSession()
    session.add(row)

    assert len(session.added) == 1
    assert session.added[0] is row
