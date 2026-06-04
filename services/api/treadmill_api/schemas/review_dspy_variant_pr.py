"""Pydantic v2 schema for the ADR-0070 ReviewDspyVariantPr record.

This schema validates dspy-variant-pr review rows at the API seam. Closed
``Literal`` enums enforce the label / confidence / outcome taxonomy; the
label-request model_validator enforces a positive cross-field invariant on
``label_override_reason``. Scores are ``float`` here (matches the TS ``number``
type and avoids Pydantic v2's default Decimal→string JSON serialization); the
ORM column stays ``Numeric(5,4)`` so DB precision is preserved on write.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LlmLabelT = Literal["merge", "revise", "drop"]
ConfidenceT = Literal["high", "medium", "low"]
OutcomeStateT = Literal[
    "pending", "merged", "rejected", "superseded", "cancelled"
]


class ReviewDspyVariantPr(BaseModel):
    """Structured row from the ``review_dspy_variant_pr`` table (ADR-0070).

    ``extra='forbid'`` ensures upstream emitters don't smuggle undeclared
    fields through this seam. ``from_attributes=True`` enables direct ORM-row
    → Pydantic conversion in the router/repository layer.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    # ── Provenance ────────────────────────────────────────────────────────────
    id: uuid.UUID
    created_at: datetime | None = None
    source_run_id: uuid.UUID
    source_pr_number: int
    source_pr_url: str

    # ── Candidate content ─────────────────────────────────────────────────────
    judge_role: str
    judge_prompt_path: str
    current_score: float
    variant_score: float
    improvement: float
    patch_diff: str
    corpus_s3_uri: str

    # ── LLM recommendation ────────────────────────────────────────────────────
    llm_label: LlmLabelT
    llm_confidence: ConfidenceT
    llm_rationale: str
    llm_prompt_version: str
    llm_model: str

    # ── Operator label (None until reviewed) ──────────────────────────────────
    label_verdict: LlmLabelT | None = None
    label_notes: str | None = None
    label_override_reason: str | None = None

    # ── Labeled metadata (None until reviewed) ────────────────────────────────
    labeled_by: str | None = None
    labeled_at: datetime | None = None
    label_guidelines_version: str | None = None

    # ── Outcome (server-projected; None when first inserted) ──────────────────
    outcome_state: OutcomeStateT | None = None
    outcome_merged_at: datetime | None = None


class LabelDspyVariantPrRequest(BaseModel):
    """Body for ``POST /api/v1/review/dspy-variant-pr/{id}/label``.

    The schema cannot enforce "override_reason required when verdict differs
    from llm_label" because the request doesn't carry ``llm_label`` — the
    router does that cross-row check. Here we enforce the positive form: when
    ``label_override_reason`` is supplied it must be non-empty.
    """

    model_config = ConfigDict(extra="forbid")

    label_verdict: LlmLabelT
    label_notes: str | None = None
    label_override_reason: str | None = None
    labeled_by: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _check_override_reason_non_empty(self) -> "LabelDspyVariantPrRequest":
        if (
            self.label_override_reason is not None
            and self.label_override_reason.strip() == ""
        ):
            raise ValueError(
                "label_override_reason must be non-empty when supplied"
            )
        return self
