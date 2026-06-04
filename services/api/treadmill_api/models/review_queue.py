"""ReviewQueueRowMixin — the shared row shape for ADR-0070 review queues.

ADR-0070 ships pre-labeled review queues as a Treadmill primitive: every
"operator sanity-checks an LLM proposal" surface follows the same six-layer
table shape (provenance, candidate content, LLM recommendation, operator
label, labeled metadata, outcome). ``TriageFindingRow`` (ADR-0061) is the
precedent this mixin generalizes — the six layers come straight off that
table, with the per-kind candidate columns and the per-kind verdict enum
left to subclasses.

The mixin does NOT inherit ``Base``. Subclasses combine it with ``Base`` in
their own ``models/<kind>.py`` and supply: a ``__tablename__``, a typed
``llm_label`` column (per-kind enum), a typed operator-verdict column
(per-kind nullable enum), per-kind candidate columns, and
``__table_args__`` extended with the helper-built CHECK constraints +
partial index.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column


class ReviewQueueRowMixin:
    """Six-layer review-queue row shape per ADR-0070.

    Subclass contract — each kind MUST provide:

    - ``__tablename__``: the mixin does not supply one; SQLAlchemy will
      raise at declarative-class build time if it is missing.
    - ``llm_label``: the LLM's recommended verdict column, typed per-kind
      (each kind has its own closed verdict enum, so the mixin cannot
      pick a column name + type without locking everyone to one shape).
    - The operator's verdict column, typed per-kind (nullable, written
      by the labeling UI). Named per-kind so the partial-index helper
      below can take the column name as an argument.
    - Any per-kind candidate-content columns describing what is being
      labeled (e.g., ``decision_id`` + ``rationale_excerpt`` for
      architect-gold; ``observation`` + ``screenshot_uri`` for triage).
    - ``__table_args__`` extended with this mixin's helper-built
      CHECK constraints + partial unlabeled index, plus any per-kind
      constraints / indexes the subclass needs.
    """

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
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── LLM recommendation (sans the per-kind verdict column) ─────────────────
    llm_confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    llm_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    llm_prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    llm_model: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Operator-label metadata (the verdict column itself is per-kind) ───────
    label_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    labeled_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    labeled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    label_guidelines_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Outcome ───────────────────────────────────────────────────────────────
    outcome_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_pr_merged_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # Closed enums — exported so subclasses build matching CHECK constraints
    # without re-declaring the value list (drift trap).
    LLM_CONFIDENCE_VALUES: tuple[str, ...] = ("high", "medium", "low")
    OUTCOME_STATE_VALUES: tuple[str, ...] = (
        "pending",
        "merged",
        "rejected",
        "superseded",
        "cancelled",
    )

    @classmethod
    def review_queue_check_constraints(
        cls, *, table_name: str
    ) -> tuple[CheckConstraint, ...]:
        """Return the two closed-enum CHECKs every kind appends to ``__table_args__``.

        Naming convention mirrors ADR-0061's ``ck_<table>_<column>`` so the
        constraint names line up across kinds for grep-ability.
        """
        confidence_values = ", ".join(f"'{v}'" for v in cls.LLM_CONFIDENCE_VALUES)
        outcome_values = ", ".join(f"'{v}'" for v in cls.OUTCOME_STATE_VALUES)
        return (
            CheckConstraint(
                f"llm_confidence IN ({confidence_values})",
                name=f"ck_{table_name}_llm_confidence",
            ),
            CheckConstraint(
                f"outcome_state IS NULL OR outcome_state IN ({outcome_values})",
                name=f"ck_{table_name}_outcome_state",
            ),
        )

    @classmethod
    def unlabeled_index(cls, *, table_name: str, verdict_column: str) -> Index:
        """Return the partial ``<verdict_column> IS NULL`` index.

        Mirrors ``ix_triage_findings_unlabeled`` from ADR-0061: keeps the
        labeling-UI "next unlabeled" query constant-time over a table where
        the vast majority of rows are labeled.
        """
        return Index(
            f"ix_{table_name}_unlabeled",
            verdict_column,
            postgresql_where=text(f"{verdict_column} IS NULL"),
        )
