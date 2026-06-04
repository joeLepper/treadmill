"""Behavioral tests for ``ReviewQueueRowMixin`` (ADR-0070 substep 1.1).

The mixin is exercised against a synthetic ``_FakeKindRow`` declared at
import time inside this module. No real Postgres — these are structural
assertions over ``__table__``, the helper-built constraints, and the
exported enum tuples.
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base
from treadmill_api.models import ReviewQueueRowMixin
from treadmill_api.models.triage_finding import TriageFindingRow

# Module-distinct __tablename__ keeps Task 1 and Task 2's synthetic kinds
# coexisting under the same ``Base.metadata`` without a "Table already
# defined" collision when pytest collects both modules in the same process.
_FAKE_TABLE = "_fake_review_kind_mixin_test"


class _FakeKindRow(ReviewQueueRowMixin, Base):
    """Synthetic subclass — the smallest shape a real kind must declare."""

    __tablename__ = _FAKE_TABLE

    # Per-kind LLM verdict column (each kind's enum is its own).
    llm_label: Mapped[str] = mapped_column(Text, nullable=False)

    # Operator's verdict column (nullable until reviewed).
    label_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-kind candidate content.
    candidate_text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        *ReviewQueueRowMixin.review_queue_check_constraints(table_name=_FAKE_TABLE),
        ReviewQueueRowMixin.unlabeled_index(
            table_name=_FAKE_TABLE, verdict_column="label_verdict"
        ),
    )


def test_mixin_supplies_provenance_layer():
    cols = _FakeKindRow.__table__.columns

    assert "id" in cols
    assert isinstance(cols["id"].type, UUID)
    assert cols["id"].primary_key
    assert cols["id"].server_default is not None

    assert "created_at" in cols
    assert isinstance(cols["created_at"].type, TIMESTAMP)
    assert cols["created_at"].type.timezone
    assert cols["created_at"].nullable is False
    assert cols["created_at"].server_default is not None

    assert isinstance(cols["source_run_id"].type, UUID)
    assert cols["source_run_id"].nullable is True
    assert isinstance(cols["source_event_id"].type, UUID)
    assert cols["source_event_id"].nullable is True
    assert isinstance(cols["source_url"].type, Text)
    assert cols["source_url"].nullable is True
    assert cols["source_pr_number"].nullable is True
    # source_pr_number is an Integer — python_type is int.
    assert cols["source_pr_number"].type.python_type is int


def test_mixin_supplies_llm_recommendation_layer():
    cols = _FakeKindRow.__table__.columns

    for name in ("llm_confidence", "llm_rationale", "llm_prompt_version", "llm_model"):
        assert name in cols, f"missing LLM recommendation column {name!r}"
        assert cols[name].nullable is False, f"{name} must be NOT NULL"

    # Confidence is a String(8); the rest are Text.
    assert cols["llm_confidence"].type.length == 8
    for name in ("llm_rationale", "llm_prompt_version", "llm_model"):
        assert isinstance(cols[name].type, Text)


def test_mixin_supplies_label_metadata_layer():
    cols = _FakeKindRow.__table__.columns

    for name in (
        "label_notes",
        "label_override_reason",
        "labeled_by",
        "label_guidelines_version",
    ):
        assert name in cols, f"missing label-metadata column {name!r}"
        assert isinstance(cols[name].type, Text)
        assert cols[name].nullable is True

    assert isinstance(cols["labeled_at"].type, TIMESTAMP)
    assert cols["labeled_at"].type.timezone
    assert cols["labeled_at"].nullable is True


def test_mixin_supplies_outcome_layer():
    cols = _FakeKindRow.__table__.columns

    assert cols["outcome_state"].type.length == 16
    assert cols["outcome_state"].nullable is True
    assert isinstance(cols["outcome_pr_merged_at"].type, TIMESTAMP)
    assert cols["outcome_pr_merged_at"].type.timezone
    assert cols["outcome_pr_merged_at"].nullable is True


def test_helper_builds_named_check_constraints():
    check_names = {
        c.name
        for c in _FakeKindRow.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert f"ck_{_FAKE_TABLE}_llm_confidence" in check_names
    assert f"ck_{_FAKE_TABLE}_outcome_state" in check_names


def test_helper_builds_partial_unlabeled_index():
    indexes = {idx.name: idx for idx in _FakeKindRow.__table__.indexes}
    assert f"ix_{_FAKE_TABLE}_unlabeled" in indexes
    idx = indexes[f"ix_{_FAKE_TABLE}_unlabeled"]

    where = idx.dialect_options["postgresql"].get("where")
    assert where is not None, "unlabeled index must carry a partial WHERE"
    assert str(where) == "label_verdict IS NULL", (
        f"partial WHERE must reference verdict column verbatim, got {where!r}"
    )
    assert [c.name for c in idx.columns] == ["label_verdict"]


def test_unlabeled_index_helper_references_supplied_verdict_column():
    """The helper accepts any verdict column name — pin the WHERE-clause
    derivation so subclasses can name the column whatever their kind uses."""
    idx = ReviewQueueRowMixin.unlabeled_index(
        table_name="some_kind", verdict_column="label_decision"
    )
    assert idx.name == "ix_some_kind_unlabeled"
    where = idx.dialect_options["postgresql"].get("where")
    # Compare against ``text(...)`` round-trip rather than string equality so
    # the assertion stays robust to ``str(TextClause)`` formatting changes.
    assert str(where) == str(text("label_decision IS NULL"))


def test_llm_confidence_values_pin_closed_enum():
    assert ReviewQueueRowMixin.LLM_CONFIDENCE_VALUES == ("high", "medium", "low")


def test_outcome_state_values_mirror_adr_0061():
    """ADR-0061's outcome enum is the canonical shape; new kinds must not drift."""
    assert ReviewQueueRowMixin.OUTCOME_STATE_VALUES == (
        "pending",
        "merged",
        "rejected",
        "superseded",
        "cancelled",
    )
    # Cross-check against TriageFindingRow's CHECK so a future schema edit
    # to the triage table without a mixin update fails this test.
    triage_check = next(
        c
        for c in TriageFindingRow.__table__.constraints
        if isinstance(c, CheckConstraint)
        and c.name == "ck_triage_findings_outcome_state"
    )
    sql = str(triage_check.sqltext)
    for value in ReviewQueueRowMixin.OUTCOME_STATE_VALUES:
        assert f"'{value}'" in sql, (
            f"outcome enum {value!r} from mixin missing from triage CHECK — drift"
        )


def test_subclass_omitting_tablename_raises():
    """The mixin deliberately does not supply ``__tablename__``; SQLAlchemy
    rejects the declarative class build when it's missing."""
    with pytest.raises(InvalidRequestError):

        class _MissingTablename(ReviewQueueRowMixin, Base):  # noqa: N801
            llm_label: Mapped[str] = mapped_column(Text, nullable=False)
            label_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)


def test_fake_kind_row_carries_six_mixin_columns_plus_per_kind_columns():
    """The mixin contributes six provenance / LLM / label-metadata / outcome
    column buckets; the subclass adds its own verdict + candidate columns."""
    cols = set(_FakeKindRow.__table__.columns.keys())
    mixin_supplied = {
        # Provenance.
        "id",
        "created_at",
        "source_run_id",
        "source_event_id",
        "source_url",
        "source_pr_number",
        # LLM recommendation (sans verdict).
        "llm_confidence",
        "llm_rationale",
        "llm_prompt_version",
        "llm_model",
        # Label metadata (sans the per-kind verdict column).
        "label_notes",
        "label_override_reason",
        "labeled_by",
        "labeled_at",
        "label_guidelines_version",
        # Outcome.
        "outcome_state",
        "outcome_pr_merged_at",
    }
    per_kind = {"llm_label", "label_verdict", "candidate_text"}
    assert mixin_supplied <= cols, (
        f"mixin missing expected columns: {mixin_supplied - cols}"
    )
    assert per_kind <= cols
    assert cols == mixin_supplied | per_kind
