"""Structured envelope for the architect-kind role's terminal output.

Per ADR-0032, the role-architect role returns a Pydantic-validated
``ArchitectVerdict`` envelope patterned on ADR-0027's ``ReviewVerdict``.
The verdict is a closed Literal so Pydantic rejects anything outside
the value-set. The four verdicts (amend / supersede / accept-as-is /
uncertain) route to different downstream handlers in ``wf-architecture-resolve``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ArchitectVerdict(BaseModel):
    """Structured envelope for the architect-kind role's terminal output.

    Per ADR-0032 Q32.d: Pydantic-validated envelope with a closed
    ``Literal`` verdict value-set. The four verdicts route to different
    downstream workflow steps in ``wf-architecture-resolve``.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["amend", "supersede", "accept-as-is", "uncertain"]
    reasoning: str = Field(..., description="the why behind the verdict")
    target_artifact: str = Field(..., description="path to ADR/plan/component")
    remediation_summary: str | None = Field(
        None, description="populated for amend / supersede verdicts"
    )
