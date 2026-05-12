"""Normalize raw GitHub webhook payloads into internal (entity_type, action,
payload_dict) shapes per ADR-0007.

GitHub fires many event types we don't care about; this module silently
returns ``None`` for those (the router responds 200 + ``status: skipped``
to keep GitHub's delivery dashboard clean).

The normalized shapes match the Pydantic event payload models in
``treadmill_api.events`` exactly — the router validates the dict through
``parse_payload`` before persisting, so any drift between this normalizer
and the registry is caught at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class NormalizationResult:
    """The normalized form of a recognized GitHub webhook event."""

    entity_type: str
    """Always ``"github"`` for normalized webhook events."""

    action: str
    """The internal verb: ``pr_opened``, ``pr_synchronize``, ``pr_merged``,
    ``pr_review_submitted``, ``check_run_completed``."""

    payload: dict[str, Any]
    """Fields matching the corresponding Pydantic event model's shape."""

    repo: str
    """Convenience: the ``org/repo`` slug, also present in payload."""

    pr_number: int | None
    """Convenience: PR number when applicable, also present in payload."""


def normalize_github_event(
    github_event: str, body: dict[str, Any]
) -> NormalizationResult | None:
    """Map a GitHub webhook to a normalized event, or ``None`` to skip.

    Args:
        github_event: Value of the ``X-GitHub-Event`` header (e.g.
            ``pull_request``, ``check_run``).
        body: Parsed JSON request body.
    """
    if github_event == "pull_request":
        return _normalize_pull_request(body)
    if github_event == "pull_request_review":
        return _normalize_pull_request_review(body)
    if github_event == "check_run":
        return _normalize_check_run(body)
    # Other events (push, issues, ping, etc.) are silently skipped.
    return None


def _normalize_pull_request(body: dict[str, Any]) -> NormalizationResult | None:
    action = body.get("action")
    pr = body.get("pull_request") or {}
    repo_full = (body.get("repository") or {}).get("full_name") or ""
    sender = (body.get("sender") or {}).get("login") or ""
    head = pr.get("head") or {}

    if action == "opened":
        return NormalizationResult(
            entity_type="github",
            action="pr_opened",
            payload={
                "repo": repo_full,
                "pr_number": int(pr.get("number") or 0),
                "sender": sender,
                "title": pr.get("title") or "",
                "head_branch": head.get("ref") or "",
                "head_sha": head.get("sha") or "",
            },
            repo=repo_full,
            pr_number=int(pr.get("number") or 0),
        )

    if action == "synchronize":
        return NormalizationResult(
            entity_type="github",
            action="pr_synchronize",
            payload={
                "repo": repo_full,
                "pr_number": int(pr.get("number") or 0),
                "sender": sender,
                "head_sha": head.get("sha") or "",
                "before_sha": body.get("before"),
            },
            repo=repo_full,
            pr_number=int(pr.get("number") or 0),
        )

    if action == "closed" and pr.get("merged"):
        return NormalizationResult(
            entity_type="github",
            action="pr_merged",
            payload={
                "repo": repo_full,
                "pr_number": int(pr.get("number") or 0),
                "sender": sender,
                "merged_sha": pr.get("merge_commit_sha"),
            },
            repo=repo_full,
            pr_number=int(pr.get("number") or 0),
        )

    # Other pull_request actions (closed-without-merge, edited, labeled,
    # reopened, etc.) are skipped at v0.
    return None


def _normalize_pull_request_review(body: dict[str, Any]) -> NormalizationResult | None:
    if body.get("action") != "submitted":
        return None
    review = body.get("review") or {}
    pr = body.get("pull_request") or {}
    repo_full = (body.get("repository") or {}).get("full_name") or ""
    sender = (body.get("sender") or {}).get("login") or ""

    return NormalizationResult(
        entity_type="github",
        action="pr_review_submitted",
        payload={
            "repo": repo_full,
            "pr_number": int(pr.get("number") or 0),
            "sender": sender,
            "state": review.get("state") or "",
            "body": review.get("body"),
        },
        repo=repo_full,
        pr_number=int(pr.get("number") or 0),
    )


def _normalize_check_run(body: dict[str, Any]) -> NormalizationResult | None:
    if body.get("action") != "completed":
        return None
    check_run = body.get("check_run") or {}
    repo_full = (body.get("repository") or {}).get("full_name") or ""
    pull_requests = check_run.get("pull_requests") or []
    pr_number = (
        int(pull_requests[0].get("number"))
        if pull_requests and pull_requests[0].get("number") is not None
        else None
    )

    return NormalizationResult(
        entity_type="github",
        action="check_run_completed",
        payload={
            "repo": repo_full,
            "pr_number": pr_number,
            "check_name": check_run.get("name") or "",
            "conclusion": check_run.get("conclusion") or "",
            "head_sha": check_run.get("head_sha") or "",
        },
        repo=repo_full,
        pr_number=pr_number,
    )
