"""Structured envelope for the architect-kind role's terminal output.

Per ADR-0032, the role-architect role returns a Pydantic-validated
``ArchitectVerdict`` envelope patterned on ADR-0027's ``ReviewVerdict``.
The verdict is a closed Literal so Pydantic rejects anything outside
the value-set. The four verdicts (amend / supersede / accept-as-is /
gate-broken) route to different downstream handlers in
``wf-architecture-resolve``.

Per ADR-0048, the prior ``uncertain`` verdict was removed: the architect
must always commit to one of the actionable verdicts.

Per ADR-0048, ``supersede`` was also repurposed: the architect now
declares the plan-text itself was wrong (not just the code). When
``verdict='supersede'`` the envelope MUST carry ``rewritten_description``
— the corrected task description that becomes the child task's text.
The supersede trigger reads this field; without it the trigger has no
text to put on the child task row, so a missing-field supersede is a
parse failure (forced ``ValueError`` by the validator below) rather than
a silent dispatch with empty description.

Per ADR-0058, ``gate-broken`` was added: the architect declares the
deterministic validation gate is failing for reasons outside the
author's control (the worker sandbox can't satisfy the gate's tooling
requirements — missing Python deps, ``cdk synth`` without
``aws-cdk-lib``, ``docker`` calls in a daemonless sandbox, network-
egress requirements, etc.). Emitted instead of ``amend`` when the
architect's classifier recognizes a ralph-loop deadlock; the dispatch
handler escalates to operator on first detection (without consuming
the amend-cap counter) and parks the run. The downstream wiring for
this verdict is staged behind this PR — Step 1 of the ADR-0058 plan
ships only the schema + parser; dispatch + event handling land in
Step 3.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArchitectVerdict(BaseModel):
    """Structured envelope for the architect-kind role's terminal output.

    Per ADR-0032 Q32.d: Pydantic-validated envelope with a closed
    ``Literal`` verdict value-set. The four verdicts route to different
    downstream workflow steps in ``wf-architecture-resolve``.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["amend", "supersede", "accept-as-is", "gate-broken"]
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
    gate_log_excerpt: str | None = Field(
        None,
        max_length=4000,
        description=(
            "Required for ``verdict='gate-broken'`` (ADR-0058). The "
            "deterministic gate's stderr/stdout excerpt that the architect "
            "is citing as evidence of a ralph-loop deadlock. Populated "
            "from the failing validation result's ``log_excerpt`` field so "
            "the operator sees the actual tooling failure without "
            "re-running the loop. Capped at 4000 chars (same bound as "
            "ReviewVerdict.rationale)."
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

    @model_validator(mode="after")
    def _gate_broken_requires_log_excerpt(self) -> "ArchitectVerdict":
        """Enforce ``gate_log_excerpt`` presence when verdict is
        ``gate-broken`` (ADR-0058). The whole point of the gate-broken
        verdict is to surface the failing tooling stderr to the operator
        on detection; without it, the operator has no diagnostic
        leverage and we've moved the wedge from the loop to the queue
        without reducing the operator's cost. Treat as parse failure so
        the architect re-runs with the excerpt populated.
        """
        if self.verdict == "gate-broken" and not (
            self.gate_log_excerpt and self.gate_log_excerpt.strip()
        ):
            raise ValueError(
                "ArchitectVerdict.verdict='gate-broken' requires a non-empty "
                "``gate_log_excerpt`` (the deterministic gate's stderr/stdout "
                "excerpt that evidences the ralph-loop deadlock). Per "
                "ADR-0058, the architect cites the failing tooling output "
                "verbatim so the operator can repair the gate without "
                "re-running the loop."
            )
        return self
