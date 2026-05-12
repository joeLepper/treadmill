"""Unit tests for ``pull_request:closed:merged`` normalization (ADR-0021).

Distinct from ``test_webhook_normalize.py``'s broader normalizer coverage:
these tests pin down the specific payload shape the plan-merge trigger
relies on (``repo`` + ``pr_number`` + ``merged_sha`` + ``sender``).
"""

from __future__ import annotations

from treadmill_api.events import parse_payload
from treadmill_api.webhooks.normalize import normalize_github_event


def _merged_pr_payload(
    *,
    repo: str = "joeLepper/treadmill",
    pr_number: int = 17,
    sender: str = "joeLepper",
    merge_commit_sha: str = "cafecafe" * 5,
    head_branch: str = "task/plan-doc",
    head_sha: str = "deadbeef" * 5,
) -> dict:
    return {
        "action": "closed",
        "pull_request": {
            "number": pr_number,
            "title": "Add plan doc",
            "merged": True,
            "merge_commit_sha": merge_commit_sha,
            "head": {"ref": head_branch, "sha": head_sha},
        },
        "repository": {"full_name": repo},
        "sender": {"login": sender},
    }


def test_pr_merged_emits_pr_merged_verb():
    body = _merged_pr_payload()
    result = normalize_github_event("pull_request", body)
    assert result is not None
    assert result.entity_type == "github"
    assert result.action == "pr_merged"


def test_pr_merged_payload_carries_repo_pr_number_and_sha():
    """ADR-0021's plan-merge trigger reads these three fields off the
    normalized payload. They must be present and correctly populated."""
    body = _merged_pr_payload(
        repo="joeLepper/treadmill",
        pr_number=42,
        merge_commit_sha="aabbccdd" * 5,
    )
    result = normalize_github_event("pull_request", body)
    assert result is not None
    assert result.payload["repo"] == "joeLepper/treadmill"
    assert result.payload["pr_number"] == 42
    assert result.payload["merged_sha"] == "aabbccdd" * 5
    # ``sender`` is the PR author / merge committer; ADR-0021 Q21.e uses
    # it as the plan's ``created_by``.
    assert result.payload["sender"] == "joeLepper"


def test_pr_merged_payload_validates_against_registered_model():
    """The plan-merge trigger reads the payload through the typed
    registry; this test enforces the contract at the normalizer boundary."""
    result = normalize_github_event(
        "pull_request", _merged_pr_payload(),
    )
    assert result is not None
    typed = parse_payload("github", "pr_merged", result.payload)
    assert typed.repo == "joeLepper/treadmill"
    assert typed.pr_number == 17
    assert typed.merged_sha == "cafecafe" * 5


def test_pr_closed_without_merged_does_not_emit_pr_merged():
    """A closed-but-not-merged PR is *not* a submission signal; the
    normalizer drops it and the plan-merge trigger never fires."""
    body = _merged_pr_payload()
    body["pull_request"]["merged"] = False
    body["pull_request"]["merge_commit_sha"] = None
    assert normalize_github_event("pull_request", body) is None


def test_pr_merged_with_missing_merge_commit_sha_still_emits():
    """GitHub may briefly return ``merge_commit_sha=null`` on the close
    event before the merge ref settles. The normalizer still emits
    ``pr_merged`` with ``merged_sha=None``; the trigger handler
    short-circuits in that case (no ref to fetch the doc at)."""
    body = _merged_pr_payload()
    body["pull_request"]["merge_commit_sha"] = None
    result = normalize_github_event("pull_request", body)
    assert result is not None
    assert result.action == "pr_merged"
    assert result.payload["merged_sha"] is None
    # Still validates against the registered model (merged_sha is
    # Optional in the schema).
    parse_payload("github", "pr_merged", result.payload)
