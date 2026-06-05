"""Create review_dspy_variant_pr table (ADR-0070 substep 4 — dspy-variant-pr review queue).

Establishes the ``review_dspy_variant_pr`` Postgres table with all six layers
(provenance, candidate content, LLM recommendation, operator label, labeled
metadata, outcome) per ADR-0070's `dspy-variant-pr` row in the kind table.

CHECK constraints enforce the closed enums (label / confidence / verdict /
outcome state) so corrupt values can never enter the corpus. The partial index
on ``label_verdict IS NULL`` keeps the labeling-UI "next unlabeled" query
constant-time, mirroring ``ix_triage_findings_unlabeled``.

Revision ID: 20260604_1200
Revises: 20260604_0100
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260604_1200"
down_revision: Union[str, Sequence[str], None] = "20260604_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "review_dspy_variant_pr",
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
        sa.Column(
            "source_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("source_pr_number", sa.Integer(), nullable=False),
        sa.Column("source_pr_url", sa.Text(), nullable=False),
        # ── Candidate content ─────────────────────────────────────────────────
        sa.Column("judge_role", sa.Text(), nullable=False),
        sa.Column("judge_prompt_path", sa.Text(), nullable=False),
        sa.Column("current_score", sa.Numeric(5, 4), nullable=False),
        sa.Column("variant_score", sa.Numeric(5, 4), nullable=False),
        sa.Column("improvement", sa.Numeric(6, 4), nullable=False),
        sa.Column("patch_diff", sa.Text(), nullable=False),
        sa.Column("corpus_s3_uri", sa.Text(), nullable=False),
        # ── LLM recommendation ────────────────────────────────────────────────
        sa.Column("llm_label", sa.String(8), nullable=False),
        sa.Column("llm_confidence", sa.String(8), nullable=False),
        sa.Column("llm_rationale", sa.Text(), nullable=False),
        sa.Column("llm_prompt_version", sa.Text(), nullable=False),
        sa.Column("llm_model", sa.Text(), nullable=False),
        # ── Operator label ────────────────────────────────────────────────────
        sa.Column("label_verdict", sa.String(8), nullable=True),
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
            "outcome_merged_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        # ── CHECK constraints ─────────────────────────────────────────────────
        sa.CheckConstraint(
            "llm_label IN ('merge', 'revise', 'drop')",
            name="ck_review_dspy_variant_pr_llm_label",
        ),
        sa.CheckConstraint(
            "llm_confidence IN ('high', 'medium', 'low')",
            name="ck_review_dspy_variant_pr_llm_confidence",
        ),
        sa.CheckConstraint(
            "label_verdict IS NULL OR label_verdict IN ('merge', 'revise', 'drop')",
            name="ck_review_dspy_variant_pr_label_verdict",
        ),
        sa.CheckConstraint(
            "outcome_state IS NULL OR outcome_state IN ("
            "'pending', 'merged', 'rejected', 'superseded', 'cancelled')",
            name="ck_review_dspy_variant_pr_outcome_state",
        ),
    )

    # ── Plain indexes ─────────────────────────────────────────────────────────
    op.create_index(
        "ix_review_dspy_variant_pr_source_pr_number",
        "review_dspy_variant_pr",
        ["source_pr_number"],
    )
    op.create_index(
        "ix_review_dspy_variant_pr_judge_role",
        "review_dspy_variant_pr",
        ["judge_role"],
    )

    # ── Partial index for O(1) "next unlabeled" query ─────────────────────────
    op.create_index(
        "ix_review_dspy_variant_pr_unlabeled",
        "review_dspy_variant_pr",
        ["label_verdict"],
        postgresql_where=sa.text("label_verdict IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_dspy_variant_pr_unlabeled",
        table_name="review_dspy_variant_pr",
    )
    op.drop_index(
        "ix_review_dspy_variant_pr_judge_role",
        table_name="review_dspy_variant_pr",
    )
    op.drop_index(
        "ix_review_dspy_variant_pr_source_pr_number",
        table_name="review_dspy_variant_pr",
    )
    op.drop_table("review_dspy_variant_pr")
