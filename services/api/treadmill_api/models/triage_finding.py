"""SQLAlchemy ORM model for the ADR-0061 triage_findings table."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class TriageFindingRow(Base):
    """Persistent record for one UI-triage finding (ADR-0061).

    Five logical layers: provenance, target state, evidence, detector output,
    dispatcher output, outcome, and nullable operator labels. CHECK constraints
    enforce closed enums; the partial index on ``label_is_real_bug IS NULL``
    keeps the labeling-UI "next unlabeled" query constant-time.
    """

    __tablename__ = "triage_findings"

    # ── Provenance ────────────────────────────────────────────────────────────
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    on_demand_request: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Target state ──────────────────────────────────────────────────────────
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    viewport_w: Mapped[int] = mapped_column(Integer, nullable=False)
    viewport_h: Mapped[int] = mapped_column(Integer, nullable=False)
    git_sha: Mapped[str] = mapped_column(Text, nullable=False)
    api_git_sha: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Evidence ──────────────────────────────────────────────────────────────
    screenshot_uri: Mapped[str] = mapped_column(Text, nullable=False)
    viewport_png_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    dom_snapshot_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    console_log_uri: Mapped[str] = mapped_column(Text, nullable=False)
    network_log_uri: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_summary: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    # ── Detector output ───────────────────────────────────────────────────────
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_pointer: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_resolution: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Dispatcher output ─────────────────────────────────────────────────────
    dispatch_action: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatch_reason: Mapped[str] = mapped_column(Text, nullable=False)
    suppression_signal: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_finding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("triage_findings.finding_id", ondelete="SET NULL"),
        nullable=True,
    )
    dispatched_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Outcome ───────────────────────────────────────────────────────────────
    outcome_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_merged_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    recurrence_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    # ── Labels (operator-set) ─────────────────────────────────────────────────
    label_is_real_bug: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    label_severity: Mapped[str | None] = mapped_column(String(8), nullable=True)
    label_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    label_fix_in_dsl: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    label_dispatch_action: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    label_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    labeled_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    labeled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    label_guidelines_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "mode IN ('periodic', 'on_demand')",
            name="ck_triage_findings_mode",
        ),
        CheckConstraint(
            "category IN ('console_error', 'network_failure', 'broken_asset', "
            "'accessibility', 'layout_overflow', 'consistency', "
            "'dead_affordance', 'loading_state', 'other')",
            name="ck_triage_findings_category",
        ),
        CheckConstraint(
            "severity IN ('high', 'medium', 'low')",
            name="ck_triage_findings_severity",
        ),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_triage_findings_confidence",
        ),
        CheckConstraint(
            "dispatch_action IN ('dispatched', 'research_only', 'suppressed', "
            "'escalated_to_operator')",
            name="ck_triage_findings_dispatch_action",
        ),
        CheckConstraint(
            "suppression_signal IS NULL OR suppression_signal IN ("
            "'duplicate_open_pr', 'duplicate_recent_finding', 'out_of_scope', "
            "'low_confidence', 'operator_action_required', 'design_intent', "
            "'not_in_design_system')",
            name="ck_triage_findings_suppression_signal",
        ),
        CheckConstraint(
            "outcome_state IS NULL OR outcome_state IN ("
            "'pending', 'merged', 'rejected', 'superseded', 'cancelled')",
            name="ck_triage_findings_outcome_state",
        ),
        Index("ix_triage_findings_run_id", "run_id"),
        Index("ix_triage_findings_prompt_version", "prompt_version"),
        Index("ix_triage_findings_target_url", "target_url"),
        Index("ix_triage_findings_dispatch_action", "dispatch_action"),
        Index(
            "ix_triage_findings_unlabeled",
            "label_is_real_bug",
            postgresql_where=text("label_is_real_bug IS NULL"),
        ),
    )
