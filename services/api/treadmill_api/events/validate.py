"""Validate-related events (ADR-0042).

ADR-0042 introduces ``validate.override`` as the consumer-emitted event
that flips ``validate_decision`` to ``'pass'`` in the mergeability VIEW
when ``role-architect`` returns ``accept-as-is`` on a ralph-loop deadlock
whose failing workflow was ``wf-validate`` (rather than ``wf-review``).

Sibling to ``review.override`` (ADR-0038): the architect's verdict is
the arbiter, and when the arbiter rules that a validate failure is
acceptable for this PR's merge, this event is how that decision reaches
the auto-merge predicate (ADR-0031 Q31.b).

The envelope mirrors ``ReviewOverride`` plus an ``override_validate_check_ids``
list so the audit trail records which specific failing checks the
architect overrode — not a blanket validate waive.
"""

from __future__ import annotations

from typing import ClassVar

from treadmill_api.events.base import EventPayload


class ValidateOverride(EventPayload):
    """Architect override of the validator's most-recent fail verdict.

    Emitted by the architect disposition (``runner_dispositions/
    architecture.py``) when ``ArchitectVerdict.verdict == 'accept-as-is'``
    on a ralph-loop deadlock whose failing workflow was ``wf-validate``
    (per ADR-0042). The consumer persists this as a normal Event row;
    the ``task_mergeability`` VIEW reads it as a validate-pass overlay
    at the matching ``commit_sha``, widening the auto-merge predicate
    from ``validate_decision='pass'`` to ``validate_decision='pass'
    OR validate_override IS NOT NULL``.

    Composes with ADR-0011 (uniform envelope), ADR-0014 (commit_sha
    plumbing), ADR-0027 (Pydantic at every boundary).
    """

    ENTITY_TYPE: ClassVar[str] = "validate"
    ACTION: ClassVar[str] = "override"

    commit_sha: str
    """The PR HEAD sha the override applies to. The mergeability VIEW's
    validate LATERAL joins on this — an override against a stale sha is
    ignored when a newer pr_synchronize lands."""

    reasoning: str
    """Architect's rationale (one paragraph) for why the validate
    failure was acceptable. Surfaced in the PR comment so operators can
    audit overrides."""

    override_validate_check_ids: list[str]
    """The specific failing ``check_id`` values the architect is
    overriding. Empty list means "all failing checks at this sha";
    populated list narrows the override to named checks so future runs
    of unrelated checks against this same sha are unaffected. The audit
    trail surface for which rules the architect waived."""
