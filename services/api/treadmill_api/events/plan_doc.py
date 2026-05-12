"""Plan-doc events emitted by the merge-to-main trigger (ADR-0021).

Distinct from ``plan.*`` events: ``plan_doc.*`` records observations about
a plan-doc *file* (its parse status, its activation marker) without
implying the Plan entity has transitioned. The merge handler persists
these for operator visibility when it chose not to dispatch (inactive
status) or could not (parse failure).
"""

from __future__ import annotations

from typing import ClassVar

from treadmill_api.events.base import EventPayload


class PlanDocObservedInactive(EventPayload):
    """The merge handler saw a plan-doc merge but its frontmatter
    ``status`` was not ``active`` — no Plan was created.

    Per ADR-0021 Q21.c: emitted so an operator can trace "Treadmill saw
    the merge and chose not to dispatch" via ``SELECT * FROM events
    WHERE entity_type='plan_doc'``.
    """

    ENTITY_TYPE: ClassVar[str] = "plan_doc"
    ACTION: ClassVar[str] = "observed_inactive"

    repo: str
    path: str
    merge_commit_sha: str
    status: str | None = None
    """The frontmatter ``status:`` value as observed (e.g. ``drafting``,
    ``completed``). ``None`` if the field was absent entirely."""

    pr_number: int | None = None


class PlanDocParseFailed(EventPayload):
    """The merge handler attempted to parse a plan-doc that had merged
    with ``status: active`` (or appeared so) but parsing raised — either
    a ``PlanDocFormatError`` (missing heading / malformed YAML) or a
    Pydantic ``ValidationError`` (schema violation).

    Per ADR-0021 "Failure path: malformed plan doc post-merge", this is
    the operator-visible record. No automatic remediation at v0; the
    operator fixes the doc and re-merges.
    """

    ENTITY_TYPE: ClassVar[str] = "plan_doc"
    ACTION: ClassVar[str] = "parse_failed"

    repo: str
    path: str
    merge_commit_sha: str
    error: str
    error_type: str
    pr_number: int | None = None
