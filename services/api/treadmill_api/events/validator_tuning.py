"""ValidatorTuning envelope for ADR-0040.

When role-architect verdicts ``accept-as-is`` on a deadlock whose
blocking signal was ``wf-validate.decision='fail'``, it also emits a
``ValidatorTuning`` proposal naming the specific rule + check that
misfired and one of three corrective actions. The coordination
consumer reads the proposal from ``StepOutput.payload.validator_tuning``
and dispatches ``wf-doc-amend`` with intent ``tune-rule-from-architect``
so an operator can review the proposed rule-YAML edit before it lands.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


class ValidatorTuning(BaseModel):
    """Architect's proposed correction to a validator rule.

    Carried in ``StepOutput.payload.validator_tuning`` when the
    architect's verdict is ``accept-as-is`` and the deadlock's blocking
    signal is a validator ``fail`` (not a reviewer ``changes_requested``).
    """

    model_config = ConfigDict(extra="forbid")

    rule_slug: str
    """Slug of the rule that misfired (e.g. 'implementation-conforms-to-diagram')."""

    check_id: str
    """The specific check within the rule that produced the false positive."""

    action: Literal["demote_severity", "narrow_applies_to", "refine_prompt"]
    """
    demote_severity  — flip the check's blocking gate to warning/advisory.
    narrow_applies_to — restrict the rule's applies_to globs.
    refine_prompt    — sharpen the check's LLM-judge prompt.
    """

    evidence: str
    """One-paragraph spec-vs-diff evidence explaining why the check misfired."""

    proposed_patch: dict[str, Any]
    """
    Action-specific patch shape:
      demote_severity:   {"from": "blocking", "to": "warning"|"advisory"}
      narrow_applies_to: {"remove_globs": [str], "keep_globs": [str]}
      refine_prompt:     {"diff_text": str}
    """

    @model_validator(mode="after")
    def _validate_patch_shape(self) -> "ValidatorTuning":
        patch = self.proposed_patch
        action = self.action

        if action == "demote_severity":
            missing = {"from", "to"} - patch.keys()
            if missing:
                raise ValueError(
                    f"demote_severity patch missing keys: {sorted(missing)}"
                )
            if patch["from"] != "blocking":
                raise ValueError(
                    "demote_severity patch 'from' must be 'blocking'"
                )
            if patch["to"] not in ("warning", "advisory"):
                raise ValueError(
                    "demote_severity patch 'to' must be 'warning' or 'advisory'"
                )

        elif action == "narrow_applies_to":
            missing = {"remove_globs", "keep_globs"} - patch.keys()
            if missing:
                raise ValueError(
                    f"narrow_applies_to patch missing keys: {sorted(missing)}"
                )
            if not isinstance(patch["remove_globs"], list):
                raise ValueError("narrow_applies_to 'remove_globs' must be a list")
            if not isinstance(patch["keep_globs"], list):
                raise ValueError("narrow_applies_to 'keep_globs' must be a list")

        elif action == "refine_prompt":
            if "diff_text" not in patch:
                raise ValueError("refine_prompt patch missing key: 'diff_text'")
            if not isinstance(patch["diff_text"], str):
                raise ValueError("refine_prompt 'diff_text' must be a string")

        return self
