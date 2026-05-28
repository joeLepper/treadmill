"""Create triage_findings table (ADR-0061 role-ui-triage schema).

Establishes the ``triage_findings`` Postgres table with all five layers
(provenance, target state, evidence, detector output, dispatcher output),
outcome projection columns, and nullable operator-label columns.

CHECK constraints enforce all closed enums in the DB so corrupt values
can never enter the corpus regardless of how the row is written. The
partial index on ``label_is_real_bug IS NULL`` keeps the labeling-UI
"next unlabeled" query constant-time.

Revision ID: 20260528_1400
Revises: 20260528_1200
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260528_1400"
down_revision: Union[str, Sequence[str], None] = "20260528_1200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "triage_findings",
        # ── Provenance ────────────────────────────────────────────────────────
        sa.Column(
            "finding_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("on_demand_request", sa.Text(), nullable=True),
        # ── Target state ──────────────────────────────────────────────────────
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("viewport_w", sa.Integer(), nullable=False),
        sa.Column("viewport_h", sa.Integer(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=False),
        sa.Column("api_git_sha", sa.Text(), nullable=True),
        # ── Evidence ──────────────────────────────────────────────────────────
        sa.Column("screenshot_uri", sa.Text(), nullable=False),
        sa.Column("viewport_png_uri", sa.Text(), nullable=True),
        sa.Column("dom_snapshot_uri", sa.Text(), nullable=True),
        sa.Column("console_log_uri", sa.Text(), nullable=False),
        sa.Column("network_log_uri", sa.Text(), nullable=False),
        sa.Column(
            "evidence_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # ── Detector output ───────────────────────────────────────────────────
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("confidence", sa.String(8), nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("evidence_pointer", sa.Text(), nullable=False),
        sa.Column("proposed_resolution", sa.Text(), nullable=False),
        # ── Dispatcher output ─────────────────────────────────────────────────
        sa.Column("dispatch_action", sa.String(32), nullable=False),
        sa.Column("dispatch_reason", sa.Text(), nullable=False),
        sa.Column("suppression_signal", sa.String(32), nullable=True),
        sa.Column(
            "parent_finding_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("triage_findings.finding_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "dispatched_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # ── Outcome ───────────────────────────────────────────────────────────
        sa.Column("outcome_state", sa.String(16), nullable=True),
        sa.Column("outcome_pr_number", sa.Integer(), nullable=True),
        sa.Column(
            "outcome_merged_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "recurrence_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # ── Labels ────────────────────────────────────────────────────────────
        sa.Column("label_is_real_bug", sa.Boolean(), nullable=True),
        sa.Column("label_severity", sa.String(8), nullable=True),
        sa.Column("label_category", sa.String(32), nullable=True),
        sa.Column("label_fix_in_dsl", sa.Boolean(), nullable=True),
        sa.Column("label_dispatch_action", sa.String(32), nullable=True),
        sa.Column("label_notes", sa.Text(), nullable=True),
        sa.Column("labeled_by", sa.Text(), nullable=True),
        sa.Column(
            "labeled_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("label_guidelines_version", sa.Text(), nullable=True),
        # ── CHECK constraints ─────────────────────────────────────────────────
        sa.CheckConstraint(
            "mode IN ('periodic', 'on_demand')",
            name="ck_triage_findings_mode",
        ),
        sa.CheckConstraint(
            "category IN ('console_error', 'network_failure', 'broken_asset', "
            "'accessibility', 'layout_overflow', 'consistency', "
            "'dead_affordance', 'loading_state', 'other')",
            name="ck_triage_findings_category",
        ),
        sa.CheckConstraint(
            "severity IN ('high', 'medium', 'low')",
            name="ck_triage_findings_severity",
        ),
        sa.CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_triage_findings_confidence",
        ),
        sa.CheckConstraint(
            "dispatch_action IN ('dispatched', 'research_only', 'suppressed', "
            "'escalated_to_operator')",
            name="ck_triage_findings_dispatch_action",
        ),
        sa.CheckConstraint(
            "suppression_signal IS NULL OR suppression_signal IN ("
            "'duplicate_open_pr', 'duplicate_recent_finding', 'out_of_scope', "
            "'low_confidence', 'operator_action_required', 'design_intent', "
            "'not_in_design_system')",
            name="ck_triage_findings_suppression_signal",
        ),
        sa.CheckConstraint(
            "outcome_state IS NULL OR outcome_state IN ("
            "'pending', 'merged', 'rejected', 'superseded', 'cancelled')",
            name="ck_triage_findings_outcome_state",
        ),
    )

    # ── Plain indexes ─────────────────────────────────────────────────────────
    op.create_index("ix_triage_findings_run_id", "triage_findings", ["run_id"])
    op.create_index(
        "ix_triage_findings_prompt_version", "triage_findings", ["prompt_version"]
    )
    op.create_index(
        "ix_triage_findings_target_url", "triage_findings", ["target_url"]
    )
    op.create_index(
        "ix_triage_findings_dispatch_action",
        "triage_findings",
        ["dispatch_action"],
    )

    # ── Partial index for O(1) "next unlabeled" query ─────────────────────────
    op.create_index(
        "ix_triage_findings_unlabeled",
        "triage_findings",
        ["label_is_real_bug"],
        postgresql_where=sa.text("label_is_real_bug IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_triage_findings_unlabeled", table_name="triage_findings")
    op.drop_index(
        "ix_triage_findings_dispatch_action", table_name="triage_findings"
    )
    op.drop_index("ix_triage_findings_target_url", table_name="triage_findings")
    op.drop_index(
        "ix_triage_findings_prompt_version", table_name="triage_findings"
    )
    op.drop_index("ix_triage_findings_run_id", table_name="triage_findings")
    op.drop_table("triage_findings")
