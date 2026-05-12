"""GitHub webhook event payloads.

Per ADR-0007, raw GitHub webhook events are normalized to internal verbs
on ingestion (``pull_request opened`` → ``pr_opened``, etc.) and the
event row's ``payload`` carries the normalized fields below — never the
raw GitHub payload.
"""

from __future__ import annotations

from typing import ClassVar

from treadmill_api.events.base import EventPayload


class GithubPrOpened(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "github"
    ACTION: ClassVar[str] = "pr_opened"

    repo: str
    pr_number: int
    sender: str
    title: str
    head_branch: str
    head_sha: str


class GithubPrSynchronize(EventPayload):
    """A new commit was pushed to an open PR's head branch.

    Fires from GitHub's ``pull_request`` webhook with action=``synchronize``.
    Per ADR-0013, this event's ``head_sha`` becomes the task's new HEAD;
    prior thumbs (review, validate, ci) at the old SHA are invalidated
    by the mergeability VIEW's construction (it joins on the latest SHA).
    """

    ENTITY_TYPE: ClassVar[str] = "github"
    ACTION: ClassVar[str] = "pr_synchronize"

    repo: str
    pr_number: int
    sender: str
    head_sha: str
    before_sha: str | None = None


class GithubPrMerged(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "github"
    ACTION: ClassVar[str] = "pr_merged"

    repo: str
    pr_number: int
    sender: str
    merged_sha: str | None = None


class GithubPrReviewSubmitted(EventPayload):
    ENTITY_TYPE: ClassVar[str] = "github"
    ACTION: ClassVar[str] = "pr_review_submitted"

    repo: str
    pr_number: int
    sender: str
    state: str
    """``approved`` | ``changes_requested`` | ``commented``."""

    body: str | None = None


class GithubCheckRunCompleted(EventPayload):
    """A CI check completed (success, failure, or cancelled)."""

    ENTITY_TYPE: ClassVar[str] = "github"
    ACTION: ClassVar[str] = "check_run_completed"

    repo: str
    pr_number: int | None = None
    """May be ``None`` for check runs not associated with a PR."""

    check_name: str
    conclusion: str
    """``success`` | ``failure`` | ``neutral`` | ``cancelled`` | ``timed_out``
    | ``action_required`` | ``stale``."""

    head_sha: str


class GithubPrConflict(EventPayload):
    """A PR was detected as merge-conflicting against its base branch.

    Emitted by the conflict-detection sweep (after a ``pr_merged`` event
    fires for any open PR in the repo). Per ADR-0013, this is the
    conflict signal for the mergeability VIEW: when ``is_conflicting``
    is true at HEAD, the VIEW resolves to ``blocked-on-conflict``.

    A subsequent successful push to the PR's branch (which triggers
    ``pr_synchronize``) invalidates this signal — the VIEW joins on
    ``commit_sha = head_sha`` and the new HEAD doesn't have a conflict
    event until the next sweep runs.
    """

    ENTITY_TYPE: ClassVar[str] = "github"
    ACTION: ClassVar[str] = "pr_conflict"

    repo: str
    pr_number: int
    head_sha: str
    is_conflicting: bool
    """Always ``True`` when emitted; the field name makes the VIEW's
    filter clause readable (``WHERE is_conflicting IS TRUE``)."""
