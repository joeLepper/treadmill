"""SQLAlchemy ORM model for the ADR-0070 architect_gold_rows table.

ADR-0070 defines the six-layer review-queue row shape that every
"operator sanity-checks an LLM proposal" surface follows:

  1. Provenance — ``id``, ``created_at``, nullable ``source_*`` fields.
  2. Candidate content — the architect's original decision captured for
     labeling: ``decision_id``, ``verdict_emitted``, ``rationale_excerpt``,
     and optional ``gate_log_uri``.
  3. LLM recommendation — the automated pre-label: ``llm_label``,
     ``llm_confidence``, ``llm_rationale``, ``llm_prompt_version``,
     ``llm_model``. Provenance + LLM columns (except ``llm_label``) are
     supplied by :class:`~treadmill_api.models.review_queue.ReviewQueueRowMixin`.
  4. Operator label — nullable until reviewed: ``label_verdict``,
     ``label_notes``, ``label_override_reason``.
  5. Labeled metadata — attribution: ``labeled_by``, ``labeled_at``,
     ``label_guidelines_version``. All nullable; supplied by the mixin.
  6. Outcome — optional projection: ``outcome_state``,
     ``outcome_pr_merged_at``. Supplied by the mixin.

CHECK constraints enforce all closed enums. The partial index on
``label_verdict IS NULL`` keeps the labeling-UI "next unlabeled" query
constant-time regardless of corpus size.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base
from treadmill_api.models.review_queue import ReviewQueueRowMixin

_TABLE = "architect_gold_rows"

VERDICT_EMITTED_VALUES: tuple[str, ...] = ("accept-as-is", "amend", "gate-broken")
LLM_LABEL_VALUES: tuple[str, ...] = ("too-permissive", "too-strict", "correct", "exclude")


class ArchitectGoldRow(ReviewQueueRowMixin, Base):
    """Persistent record for one architect-gold labeling candidate (ADR-0070).

    ``ArchitectGoldRow`` satisfies the ADR-0070 six-layer contract by
    mixing in :class:`ReviewQueueRowMixin` (provenance, LLM recommendation
    sans verdict, labeled metadata, outcome) and adding the architect-gold-
    specific candidate-content columns plus the per-kind verdict columns.
    """

    __tablename__ = _TABLE

    # ── Provenance (extra beyond mixin) ──────────────────────────────────────
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # ── Candidate content ─────────────────────────────────────────────────────
    decision_id: Mapped[str] = mapped_column(Text, nullable=False)
    verdict_emitted: Mapped[str] = mapped_column(String(32), nullable=False)
    rationale_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    gate_log_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── LLM recommendation (per-kind verdict column) ──────────────────────────
    llm_label: Mapped[str] = mapped_column(String(32), nullable=False)

    # ── Operator label (per-kind nullable verdict) ────────────────────────────
    label_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        # Per-kind closed-enum constraints.
        CheckConstraint(
            "verdict_emitted IN ('accept-as-is', 'amend', 'gate-broken')",
            name=f"ck_{_TABLE}_verdict_emitted",
        ),
        CheckConstraint(
            "llm_label IN ('too-permissive', 'too-strict', 'correct', 'exclude')",
            name=f"ck_{_TABLE}_llm_label",
        ),
        CheckConstraint(
            "label_verdict IS NULL OR label_verdict IN "
            "('too-permissive', 'too-strict', 'correct', 'exclude')",
            name=f"ck_{_TABLE}_label_verdict",
        ),
        # Mixin-supplied constraints (llm_confidence + outcome_state).
        *ReviewQueueRowMixin.review_queue_check_constraints(table_name=_TABLE),
        # Plain indexes.
        Index(f"ix_{_TABLE}_created_at", "created_at"),
        Index(f"ix_{_TABLE}_verdict_emitted", "verdict_emitted"),
        # Partial index for O(1) "next unlabeled" query.
        ReviewQueueRowMixin.unlabeled_index(
            table_name=_TABLE,
            verdict_column="label_verdict",
        ),
    )
