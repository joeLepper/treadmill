"""Review-related events (ADR-0038).

ADR-0038 introduces ``review.override`` as the consumer-emitted event
that flips ``review_decision`` to ``'approved'`` in the mergeability
VIEW when ``role-architect`` returns ``accept-as-is`` on a ralph-loop
deadlock. The architect's verdict is the arbiter — when it decides
the work was fine all along, this event is how that decision reaches
the gate machinery.

The envelope is intentionally minimal: ``commit_sha`` (so the VIEW's
per-commit-HEAD lateral join can match) plus a free-text
``reasoning`` field (so operators reading the PR comment understand
why the override fired).
"""

from __future__ import annotations

from typing import ClassVar

from treadmill_api.events.base import EventPayload


class ReviewOverride(EventPayload):
    """Architect override of the reviewer's most-recent verdict.

    Emitted by the architect disposition (``runner_dispositions/
    architecture.py``) when ``ArchitectVerdict.verdict == 'accept-as-is'``
    on a ralph-loop deadlock (per ADR-0038). The consumer persists this
    as a normal Event row; the ``task_mergeability`` VIEW reads it as
    ``review_decision='approved'`` at the matching ``commit_sha``.

    Composes with ADR-0011 (uniform envelope), ADR-0014 (commit_sha
    plumbing), ADR-0027 (Pydantic at every boundary).
    """

    ENTITY_TYPE: ClassVar[str] = "review"
    ACTION: ClassVar[str] = "override"

    commit_sha: str
    """The PR HEAD sha the override applies to. The mergeability VIEW's
    review LATERAL joins on this — an override against a stale sha is
    ignored when a newer pr_synchronize lands."""

    reasoning: str
    """Architect's rationale (one paragraph) for why the work was
    accept-as-is despite the reviewer's request_changes verdict.
    Surfaced in the PR comment so operators can audit overrides."""
