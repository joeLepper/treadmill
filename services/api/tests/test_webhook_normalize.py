"""Unit tests for GitHub event normalization."""

from __future__ import annotations

from treadmill_api.events import parse_payload
from treadmill_api.webhooks.persist import _extract_commit_sha
from treadmill_api.webhooks.normalize import normalize_github_event


# ── Fixture builders for raw GitHub payloads ─────────────────────────────────


def _pr_payload(action: str, *, merged: bool = False, **overrides):
    base = {
        "action": action,
        "pull_request": {
            "number": 42,
            "title": "Add /health endpoint",
            "merged": merged,
            "merge_commit_sha": "cafebabe" * 5 if merged else None,
            "head": {"ref": "task/abc-feat", "sha": "deadbeef" * 5},
        },
        "repository": {"full_name": "RAMJAC/treadmill"},
        "sender": {"login": "alice"},
    }
    base.update(overrides)
    return base


def _review_payload(state: str, body_text: str | None = None):
    return {
        "action": "submitted",
        "review": {"state": state, "body": body_text},
        "pull_request": {"number": 42},
        "repository": {"full_name": "RAMJAC/treadmill"},
        "sender": {"login": "bob"},
    }


def _check_run_payload(conclusion: str, pr_number: int | None = 42):
    pulls = [{"number": pr_number}] if pr_number is not None else []
    return {
        "action": "completed",
        "check_run": {
            "name": "ci",
            "conclusion": conclusion,
            "head_sha": "deadbeef" * 5,
            "pull_requests": pulls,
        },
        "repository": {"full_name": "RAMJAC/treadmill"},
    }


# ── pull_request → pr_opened / pr_merged ─────────────────────────────────────


def test_pull_request_opened_normalizes_to_pr_opened():
    result = normalize_github_event("pull_request", _pr_payload("opened"))
    assert result is not None
    assert result.entity_type == "github"
    assert result.action == "pr_opened"
    assert result.repo == "RAMJAC/treadmill"
    assert result.pr_number == 42
    assert result.payload["title"] == "Add /health endpoint"
    assert result.payload["head_branch"] == "task/abc-feat"
    assert result.payload["head_sha"] == "deadbeef" * 5
    # The normalized payload validates against the typed event registry.
    parse_payload("github", "pr_opened", result.payload)


def test_pull_request_closed_with_merged_normalizes_to_pr_merged():
    result = normalize_github_event("pull_request", _pr_payload("closed", merged=True))
    assert result is not None
    assert result.action == "pr_merged"
    assert result.payload["merged_sha"] == "cafebabe" * 5
    parse_payload("github", "pr_merged", result.payload)


def test_pull_request_closed_without_merged_is_skipped():
    """Closed-without-merged is not actionable at v0; the normalizer drops it."""
    assert normalize_github_event("pull_request", _pr_payload("closed", merged=False)) is None


def test_pull_request_synchronize_normalizes_to_pr_synchronize():
    """Per ADR-0014, ``pull_request.synchronize`` (new commits pushed to an
    open PR) becomes ``pr_synchronize`` with the new HEAD + prior HEAD."""
    body = _pr_payload("synchronize")
    body["before"] = "deadbeef" * 5
    body["pull_request"]["head"]["sha"] = "cafebabe" * 5
    result = normalize_github_event("pull_request", body)
    assert result is not None
    assert result.entity_type == "github"
    assert result.action == "pr_synchronize"
    assert result.repo == "RAMJAC/treadmill"
    assert result.pr_number == 42
    assert result.payload["head_sha"] == "cafebabe" * 5
    assert result.payload["before_sha"] == "deadbeef" * 5
    assert result.payload["sender"] == "alice"
    # Validates against the typed event registry.
    parse_payload("github", "pr_synchronize", result.payload)


def test_pull_request_synchronize_without_before_field():
    """The ``before`` field is best-effort; when absent, ``before_sha`` is
    ``None`` and the payload still validates."""
    body = _pr_payload("synchronize")
    # No "before" key set.
    result = normalize_github_event("pull_request", body)
    assert result is not None
    assert result.action == "pr_synchronize"
    assert result.payload["before_sha"] is None
    parse_payload("github", "pr_synchronize", result.payload)


def test_pull_request_other_actions_are_skipped():
    for action in ("edited", "labeled", "reopened", "review_requested"):
        assert normalize_github_event("pull_request", _pr_payload(action)) is None


# ── pull_request_review → pr_review_submitted ────────────────────────────────


def test_review_submitted_normalizes():
    result = normalize_github_event(
        "pull_request_review", _review_payload("changes_requested", "needs tests")
    )
    assert result is not None
    assert result.action == "pr_review_submitted"
    assert result.payload["state"] == "changes_requested"
    assert result.payload["body"] == "needs tests"
    parse_payload("github", "pr_review_submitted", result.payload)


def test_review_with_null_body():
    """Reviews can be approved without a body comment."""
    result = normalize_github_event("pull_request_review", _review_payload("approved", None))
    assert result is not None
    assert result.payload["body"] is None
    parse_payload("github", "pr_review_submitted", result.payload)


def test_review_other_actions_are_skipped():
    for action in ("edited", "dismissed"):
        body = _review_payload("approved")
        body["action"] = action
        assert normalize_github_event("pull_request_review", body) is None


# ── check_run → check_run_completed ──────────────────────────────────────────


def test_check_run_completed_with_pr_number():
    result = normalize_github_event("check_run", _check_run_payload("failure", pr_number=42))
    assert result is not None
    assert result.action == "check_run_completed"
    assert result.payload["pr_number"] == 42
    assert result.payload["conclusion"] == "failure"
    assert result.payload["check_name"] == "ci"
    parse_payload("github", "check_run_completed", result.payload)


def test_check_run_completed_without_pr_number():
    """Some check runs aren't associated with a PR (e.g. on default branch)."""
    result = normalize_github_event("check_run", _check_run_payload("success", pr_number=None))
    assert result is not None
    assert result.payload["pr_number"] is None
    parse_payload("github", "check_run_completed", result.payload)


def test_check_run_non_completed_is_skipped():
    body = _check_run_payload("success")
    body["action"] = "created"
    assert normalize_github_event("check_run", body) is None


# ── Unhandled event types ────────────────────────────────────────────────────


def test_push_event_is_skipped():
    assert normalize_github_event("push", {"ref": "refs/heads/main"}) is None


def test_ping_event_is_skipped():
    assert normalize_github_event("ping", {"zen": "Speak like a human."}) is None


def test_issues_event_is_skipped():
    assert normalize_github_event("issues", {"action": "opened"}) is None


# ── Defensive: missing fields don't crash the normalizer ─────────────────────


def test_missing_repository_yields_empty_repo_string():
    body = {"action": "opened", "pull_request": {"number": 1, "head": {}}}
    result = normalize_github_event("pull_request", body)
    assert result is not None
    assert result.repo == ""
    # Still passes the registry's pr_opened schema (which doesn't constrain
    # repo to be non-empty).
    parse_payload("github", "pr_opened", result.payload)


def test_missing_pr_number_defaults_to_zero():
    body = {
        "action": "opened",
        "pull_request": {"head": {}},
        "repository": {"full_name": "x/y"},
        "sender": {"login": "alice"},
    }
    result = normalize_github_event("pull_request", body)
    assert result is not None
    assert result.pr_number == 0


# ── _extract_commit_sha: receiver-side SHA extraction (ADR-0014) ─────────────


def test_extract_commit_sha_pr_opened():
    body = _pr_payload("opened")
    assert _extract_commit_sha("pr_opened", body) == "deadbeef" * 5


def test_extract_commit_sha_pr_synchronize():
    body = _pr_payload("synchronize")
    body["pull_request"]["head"]["sha"] = "cafebabe" * 5
    assert _extract_commit_sha("pr_synchronize", body) == "cafebabe" * 5


def test_extract_commit_sha_pr_review_submitted():
    """``pr_review_submitted`` carries the reviewed SHA in
    ``review.commit_id`` — receiver pulls from there."""
    body = {
        "action": "submitted",
        "review": {"state": "approved", "commit_id": "cafebabe" * 5},
        "pull_request": {"number": 42},
        "repository": {"full_name": "x/y"},
        "sender": {"login": "bob"},
    }
    assert _extract_commit_sha("pr_review_submitted", body) == "cafebabe" * 5


def test_extract_commit_sha_pr_merged_prefers_merge_commit_sha():
    body = _pr_payload("closed", merged=True)
    assert _extract_commit_sha("pr_merged", body) == "cafebabe" * 5


def test_extract_commit_sha_pr_merged_falls_back_to_head_sha():
    """If ``merge_commit_sha`` is absent, the receiver falls back to
    ``pull_request.head.sha``."""
    body = _pr_payload("closed", merged=True)
    body["pull_request"]["merge_commit_sha"] = None
    assert _extract_commit_sha("pr_merged", body) == "deadbeef" * 5


def test_extract_commit_sha_check_run_completed():
    body = _check_run_payload("failure")
    assert _extract_commit_sha("check_run_completed", body) == "deadbeef" * 5


def test_extract_commit_sha_returns_none_for_unknown_action():
    """Actions without a commit anchor (e.g. future ``plan.registered``
    that ever leak into the receiver) return ``None``; the column is
    nullable per ADR-0014."""
    assert _extract_commit_sha("some_other_action", {}) is None


def test_extract_commit_sha_returns_none_when_field_missing():
    """If the GitHub payload is malformed and the SHA field is absent,
    the receiver returns ``None`` rather than crashing."""
    body = {"pull_request": {"head": {}}}
    assert _extract_commit_sha("pr_opened", body) is None
    assert _extract_commit_sha("pr_synchronize", body) is None
    assert _extract_commit_sha("pr_review_submitted", {"review": {}}) is None
    assert _extract_commit_sha("check_run_completed", {"check_run": {}}) is None
