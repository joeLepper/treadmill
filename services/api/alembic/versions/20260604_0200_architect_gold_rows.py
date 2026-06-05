"""Create architect_gold_rows table (ADR-0070 §2 candidate-content layer).

Establishes the ``architect_gold_rows`` Postgres table with the ADR-0070
six-layer shape:

  1. Provenance (id, created_at, source_* nullable fields)
  2. Candidate content (decision_id, verdict_emitted, rationale_excerpt,
     gate_log_uri)
  3. LLM recommendation (llm_label, llm_confidence, llm_rationale,
     llm_prompt_version, llm_model)
  4. Operator label (label_verdict, label_notes, label_override_reason —
     all nullable)
  5. Labeled metadata (labeled_by, labeled_at, label_guidelines_version —
     all nullable)
  6. Outcome (outcome_state, outcome_pr_merged_at — both nullable)

CHECK constraints enforce all closed enums; the partial index on
``label_verdict IS NULL`` keeps the labeling-UI "next unlabeled" query
constant-time.

Revision ID: 20260604_0200
Revises: 20260604_0100
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260604_0200"
down_revision: Union[str, Sequence[str], None] = "20260604_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "architect_gold_rows",
        # ── Provenance ────────────────────────────────────────────────────────
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_pr_number", sa.Integer(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        # ── Candidate content ─────────────────────────────────────────────────
        sa.Column("decision_id", sa.Text(), nullable=False),
        sa.Column("verdict_emitted", sa.String(32), nullable=False),
        sa.Column("rationale_excerpt", sa.Text(), nullable=False),
        sa.Column("gate_log_uri", sa.Text(), nullable=True),
        # ── LLM recommendation ────────────────────────────────────────────────
        sa.Column("llm_label", sa.String(32), nullable=False),
        sa.Column("llm_confidence", sa.String(8), nullable=False),
        sa.Column("llm_rationale", sa.Text(), nullable=False),
        sa.Column("llm_prompt_version", sa.Text(), nullable=False),
        sa.Column("llm_model", sa.Text(), nullable=False),
        # ── Operator label ────────────────────────────────────────────────────
        sa.Column("label_verdict", sa.String(32), nullable=True),
        sa.Column("label_notes", sa.Text(), nullable=True),
        sa.Column("label_override_reason", sa.Text(), nullable=True),
        # ── Labeled metadata ──────────────────────────────────────────────────
        sa.Column("labeled_by", sa.Text(), nullable=True),
        sa.Column(
            "labeled_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("label_guidelines_version", sa.Text(), nullable=True),
        # ── Outcome ───────────────────────────────────────────────────────────
        sa.Column("outcome_state", sa.String(16), nullable=True),
        sa.Column(
            "outcome_pr_merged_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        # ── CHECK constraints ─────────────────────────────────────────────────
        sa.CheckConstraint(
            "verdict_emitted IN ('accept-as-is', 'amend', 'gate-broken')",
            name="ck_architect_gold_rows_verdict_emitted",
        ),
        sa.CheckConstraint(
            "llm_label IN ('too-permissive', 'too-strict', 'correct', 'exclude')",
            name="ck_architect_gold_rows_llm_label",
        ),
        sa.CheckConstraint(
            "llm_confidence IN ('high', 'medium', 'low')",
            name="ck_architect_gold_rows_llm_confidence",
        ),
        sa.CheckConstraint(
            "label_verdict IS NULL OR label_verdict IN "
            "('too-permissive', 'too-strict', 'correct', 'exclude')",
            name="ck_architect_gold_rows_label_verdict",
        ),
        sa.CheckConstraint(
            "outcome_state IS NULL OR outcome_state IN "
            "('pending', 'merged', 'rejected', 'superseded', 'cancelled')",
            name="ck_architect_gold_rows_outcome_state",
        ),
    )

    # ── Plain indexes ─────────────────────────────────────────────────────────
    op.create_index(
        "ix_architect_gold_rows_created_at",
        "architect_gold_rows",
        ["created_at"],
    )
    op.create_index(
        "ix_architect_gold_rows_verdict_emitted",
        "architect_gold_rows",
        ["verdict_emitted"],
    )

    # ── Partial index for O(1) "next unlabeled" query ─────────────────────────
    op.create_index(
        "ix_architect_gold_rows_unlabeled",
        "architect_gold_rows",
        ["label_verdict"],
        postgresql_where=sa.text("label_verdict IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_architect_gold_rows_unlabeled", table_name="architect_gold_rows"
    )
    op.drop_index(
        "ix_architect_gold_rows_verdict_emitted", table_name="architect_gold_rows"
    )
    op.drop_index(
        "ix_architect_gold_rows_created_at", table_name="architect_gold_rows"
    )
    op.drop_table("architect_gold_rows")
