"""Structured envelope for crystallization role's terminal output.

Per ADR-0032, the crystallization-kind role returns a Pydantic-validated
``CrystallizationVerdict`` envelope mirroring ``ArchitectVerdict``.
The verdict is a closed Literal so Pydantic rejects anything outside
the value-set.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CrystallizationVerdict(BaseModel):
    """Structured envelope for the crystallization-kind role's terminal output.

    Per ADR-0032: Pydantic-validated envelope with a closed
    ``Literal`` verdict value-set. The verdicts (ready / not-ready / defer)
    route to different downstream workflow steps.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["ready", "not-ready", "defer"]
    reasoning: str = Field(..., description="the why behind the verdict")
    learning_slug: str = Field(..., description="slug identifying the learning")
    proposed_rule_slug: str | None = Field(
        None, description="slug for the proposed rule, if applicable"
    )
