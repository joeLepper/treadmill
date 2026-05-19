"""Structured envelope for the architect-kind role's terminal output.

Per ADR-0032, the role-architect role returns a Pydantic-validated
``ArchitectVerdict`` envelope patterned on ADR-0027's ``ReviewVerdict``.
The verdict is a closed Literal so Pydantic rejects anything outside
the value-set. The three verdicts (amend / supersede / accept-as-is)
route to different downstream handlers in ``wf-architecture-resolve``.

Per ADR-0048, the prior ``uncertain`` verdict was removed: the architect
must always commit to one of the three actionable verdicts.

Per ADR-0048, ``supersede`` was also repurposed: the architect now
declares the plan-text itself was wrong (not just the code). When
``verdict='supersede'`` the envelope MUST carry ``rewritten_description``
— the corrected task description that becomes the child task's text.
The supersede trigger reads this field; without it the trigger has no
text to put on the child task row, so a missing-field supersede is a
parse failure (forced ``ValueError`` by the validator below) rather than
a silent dispatch with empty description.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArchitectVerdict(BaseModel):
    """Structured envelope for the architect-kind role's terminal output.

    Per ADR-0032 Q32.d: Pydantic-validated envelope with a closed
    ``Literal`` verdict value-set. The three verdicts route to different
    downstream workflow steps in ``wf-architecture-resolve``.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["amend", "supersede", "accept-as-is"]
    reasoning: str = Field(..., description="the why behind the verdict")
    target_artifact: str = Field(..., description="path to ADR/plan/component")
    remediation_summary: str | None = Field(
        None, description="populated for amend / supersede verdicts"
    )
    rewritten_description: str | None = Field(
        None,
        description=(
            "Required for ``verdict='supersede'`` (ADR-0048). The corrected "
            "task description that the supersede trigger writes onto the "
            "child task row. Task text is immutable per row — supersede "
            "creates a new row carrying this field's value, not an in-place "
            "edit of the parent."
        ),
    )

    @model_validator(mode="after")
    def _supersede_requires_rewritten_description(self) -> "ArchitectVerdict":
        """Enforce ``rewritten_description`` presence when verdict is
        ``supersede`` (ADR-0048). A supersede with no rewritten text is
        meaningless — the trigger would create a child task with the
        parent's original (already-failed) description. Treat as a parse
        failure so wf-feedback can re-run the architect with an explicit
        envelope reminder.
        """
        if self.verdict == "supersede" and not (
            self.rewritten_description and self.rewritten_description.strip()
        ):
            raise ValueError(
                "ArchitectVerdict.verdict='supersede' requires a non-empty "
                "``rewritten_description`` (the corrected task text that "
                "becomes the child task's description). Per ADR-0048, "
                "supersede creates a new task row carrying this field's "
                "value; an empty rewrite has no child-task content."
            )
        return self
