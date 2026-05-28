"""Pydantic v2 schema for the ADR-0061 TriageFinding record.

This schema validates role-ui-triage output before it is persisted.
Closed Literal enums enforce the bug taxonomy; cross-field model_validators
enforce the suppression_signal / dispatched_plan_id invariants.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CategoryT = Literal[
    "console_error",
    "network_failure",
    "broken_asset",
    "accessibility",
    "layout_overflow",
    "consistency",
    "dead_affordance",
    "loading_state",
    "other",
]
SeverityT = Literal["high", "medium", "low"]
ConfidenceT = Literal["high", "medium", "low"]
DispatchActionT = Literal[
    "dispatched",
    "research_only",
    "suppressed",
    "escalated_to_operator",
]
SuppressionSignalT = Literal[
    "duplicate_open_pr",
    "duplicate_recent_finding",
    "out_of_scope",
    "low_confidence",
    "operator_action_required",
    "design_intent",
    "not_in_design_system",
]
ModeT = Literal["periodic", "on_demand"]
OutcomeStateT = Literal["pending", "merged", "rejected", "superseded", "cancelled"]


class TriageFinding(BaseModel):
    """Structured output from role-ui-triage (ADR-0061).

    ``extra='forbid'`` ensures the role does not emit undeclared fields that
    would silently pass through and confuse downstream consumers.
    ``from_attributes=True`` enables direct ORM-row → Pydantic conversion in
    the repository layer.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    # ── Provenance ────────────────────────────────────────────────────────────
    finding_id: uuid.UUID
    run_id: uuid.UUID
    created_at: datetime | None = None
    prompt_version: str
    model: str
    mode: ModeT
    on_demand_request: str | None = None

    # ── Target state ──────────────────────────────────────────────────────────
    target_url: str
    viewport_w: int
    viewport_h: int
    git_sha: str
    api_git_sha: str | None = None

    # ── Evidence ──────────────────────────────────────────────────────────────
    screenshot_uri: str
    viewport_png_uri: str | None = None
    dom_snapshot_uri: str | None = None
    console_log_uri: str
    network_log_uri: str
    evidence_summary: dict[str, Any] = Field(default_factory=dict)

    # ── Detector output ───────────────────────────────────────────────────────
    category: CategoryT
    severity: SeverityT
    confidence: ConfidenceT
    observation: str = Field(..., max_length=240)
    evidence_pointer: str
    proposed_resolution: str = Field(..., max_length=900)

    # ── Dispatcher output ─────────────────────────────────────────────────────
    dispatch_action: DispatchActionT
    dispatch_reason: str
    suppression_signal: SuppressionSignalT | None = None
    parent_finding_id: uuid.UUID | None = None
    dispatched_plan_id: uuid.UUID | None = None

    # ── Outcome (server-projected; None when first inserted) ──────────────────
    outcome_state: OutcomeStateT | None = None
    outcome_pr_number: int | None = None
    outcome_merged_at: datetime | None = None
    recurrence_count: int = 0

    # ── Labels (operator-set; None until labeled) ─────────────────────────────
    label_is_real_bug: bool | None = None
    label_severity: SeverityT | None = None
    label_category: CategoryT | None = None
    label_fix_in_dsl: bool | None = None
    label_dispatch_action: DispatchActionT | None = None
    label_notes: str | None = None
    labeled_by: str | None = None
    labeled_at: datetime | None = None
    label_guidelines_version: str | None = None

    @model_validator(mode="after")
    def _check_suppression_signal(self) -> "TriageFinding":
        if self.dispatch_action == "suppressed" and self.suppression_signal is None:
            raise ValueError(
                'suppression_signal is required when dispatch_action is "suppressed"'
            )
        if self.dispatch_action != "suppressed" and self.suppression_signal is not None:
            raise ValueError(
                "suppression_signal must be null when dispatch_action is not "
                '"suppressed"'
            )
        return self

    @model_validator(mode="after")
    def _check_dispatched_plan_id(self) -> "TriageFinding":
        if self.dispatch_action == "dispatched" and self.dispatched_plan_id is None:
            raise ValueError(
                'dispatched_plan_id is required when dispatch_action is "dispatched"'
            )
        if (
            self.dispatch_action != "dispatched"
            and self.dispatched_plan_id is not None
        ):
            raise ValueError(
                "dispatched_plan_id must be null when dispatch_action is not "
                '"dispatched"'
            )
        return self
