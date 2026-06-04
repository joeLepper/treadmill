"""SQLAlchemy ORM model for the ADR-0070 review_dspy_variant_pr table."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class ReviewDspyVariantPrRow(Base):
    """Persistent record for one dspy-variant-pr review (ADR-0070 substep 4).

    Six logical layers: provenance, candidate content, LLM recommendation,
    operator label, labeled metadata, and outcome. CHECK constraints enforce
    closed enums; the partial index on ``label_verdict IS NULL`` keeps the
    labeling-UI "next unlabeled" query constant-time.
    """

    __tablename__ = "review_dspy_variant_pr"

    # ── Provenance ────────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    source_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    source_pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_pr_url: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Candidate content ─────────────────────────────────────────────────────
    judge_role: Mapped[str] = mapped_column(Text, nullable=False)
    judge_prompt_path: Mapped[str] = mapped_column(Text, nullable=False)
    current_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    variant_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    improvement: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    patch_diff: Mapped[str] = mapped_column(Text, nullable=False)
    corpus_s3_uri: Mapped[str] = mapped_column(Text, nullable=False)

    # ── LLM recommendation ────────────────────────────────────────────────────
    llm_label: Mapped[str] = mapped_column(String(8), nullable=False)
    llm_confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    llm_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    llm_prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    llm_model: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Operator label (nullable until reviewed) ──────────────────────────────
    label_verdict: Mapped[str | None] = mapped_column(String(8), nullable=True)
    label_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Labeled metadata ──────────────────────────────────────────────────────
    labeled_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    labeled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    label_guidelines_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Outcome (server-projected; nullable until known) ──────────────────────
    outcome_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_merged_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "llm_label IN ('merge', 'revise', 'drop')",
            name="ck_review_dspy_variant_pr_llm_label",
        ),
        CheckConstraint(
            "llm_confidence IN ('high', 'medium', 'low')",
            name="ck_review_dspy_variant_pr_llm_confidence",
        ),
        CheckConstraint(
            "label_verdict IS NULL OR label_verdict IN ('merge', 'revise', 'drop')",
            name="ck_review_dspy_variant_pr_label_verdict",
        ),
        CheckConstraint(
            "outcome_state IS NULL OR outcome_state IN ("
            "'pending', 'merged', 'rejected', 'superseded', 'cancelled')",
            name="ck_review_dspy_variant_pr_outcome_state",
        ),
        Index(
            "ix_review_dspy_variant_pr_source_pr_number",
            "source_pr_number",
        ),
        Index(
            "ix_review_dspy_variant_pr_judge_role",
            "judge_role",
        ),
        Index(
            "ix_review_dspy_variant_pr_unlabeled",
            "label_verdict",
            postgresql_where=text("label_verdict IS NULL"),
        ),
    )
