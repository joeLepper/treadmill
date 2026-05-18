"""Structured envelope for the rule-corpus-auditor role's terminal output.

The rule-corpus-auditor reads docs/knowledge-base/rules/*.yaml and
docs/learnings/*.md, evaluates each rule against four staleness criteria,
and returns a ``RuleCorpusAudit`` envelope with one entry per rule. The
status literal (keep / deprecate / update) is closed so Pydantic rejects
anything outside the value-set.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RuleCorpusAuditEntry(BaseModel):
    """Single rule evaluation result within a RuleCorpusAudit envelope."""

    model_config = ConfigDict(extra="forbid")

    rule_slug: str = Field(
        ..., description="rule filename slug, e.g. adr-and-plan-has-diagram"
    )
    status: Literal["keep", "deprecate", "update"]
    rationale: str = Field(..., description="one-sentence reason for the status")
    proposed_action: str = Field(
        ..., description="what to do, e.g. no action, remove rule file, update check.sh path"
    )


class RuleCorpusAudit(BaseModel):
    """Structured envelope for the rule-corpus-auditor role's terminal output.

    Contains one RuleCorpusAuditEntry per rule file in
    docs/knowledge-base/rules/. The status literals route to different
    downstream handlers in wf-audit-rule-corpus:
      keep       — rule is active and accurate; no action.
      deprecate  — rule is stale, superseded, or its learning is obsolete.
      update     — rule content or check.sh paths need revision.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[RuleCorpusAuditEntry]
