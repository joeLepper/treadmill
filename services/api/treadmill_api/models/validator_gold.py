"""SQLAlchemy ORM model for the ADR-0070 validator_gold_rows table.

ADR-0070 defines the six-layer review-queue row shape that every
"operator sanity-checks an LLM proposal" surface follows:

  1. Provenance — ``id``, ``created_at``, nullable ``source_*`` fields.
  2. Candidate content — the validator's verdict captured for labeling:
     ``source_step_id`` (FK to workflow_run_steps.id), ``verdict_emitted``,
     ``script_excerpt``, and ``artifact_excerpt``.
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

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base
from treadmill_api.models.review_queue import ReviewQueueRowMixin

_TABLE = "validator_gold_rows"

VERDICT_EMITTED_VALUES: tuple[str, ...] = ("pass", "fail")
LLM_LABEL_VALUES: tuple[str, ...] = ("correct-verdict", "wrong-verdict", "unclear")


class ValidatorGoldRow(ReviewQueueRowMixin, Base):
    """Persistent record for one validator-gold labeling candidate (ADR-0070).

    ``ValidatorGoldRow`` satisfies the ADR-0070 six-layer contract by
    mixing in :class:`ReviewQueueRowMixin` (provenance, LLM recommendation
    sans verdict, labeled metadata, outcome) and adding the validator-gold-
    specific candidate-content columns plus the per-kind verdict columns.
    """

    __tablename__ = _TABLE

    # ── Candidate content ─────────────────────────────────────────────────────
    source_step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_run_steps.id", ondelete="SET NULL"),
        nullable=False,
    )
    verdict_emitted: Mapped[str] = mapped_column(String(8), nullable=False)
    script_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_excerpt: Mapped[str] = mapped_column(Text, nullable=False)

    # ── LLM recommendation (per-kind verdict column) ──────────────────────────
    llm_label: Mapped[str] = mapped_column(String(32), nullable=False)

    # ── Operator label (per-kind nullable verdict) ────────────────────────────
    label_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        # Per-kind closed-enum constraints.
        CheckConstraint(
            "verdict_emitted IN ('pass', 'fail')",
            name=f"ck_{_TABLE}_verdict_emitted",
        ),
        CheckConstraint(
            "llm_label IN ('correct-verdict', 'wrong-verdict', 'unclear')",
            name=f"ck_{_TABLE}_llm_label",
        ),
        CheckConstraint(
            "label_verdict IS NULL OR label_verdict IN "
            "('correct-verdict', 'wrong-verdict', 'unclear')",
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
