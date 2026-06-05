"""Per-kind disposition handler tests (ADR-0022).

One test per handler — exercises the four kinds (``code``,
``review``, ``analysis``, ``plan_doc``) against their
``DispositionContext``, mostly via direct invocation with synthetic
contexts so the tests stay fast + deterministic.

The runner-level dispatch (the table that picks the handler) is
exercised in ``test_runner.py``.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from treadmill_agent import claude_code, gh, git
from treadmill_agent.api_client import Role, WorkerContext
from treadmill_agent.claude_code import CodeAuthorResult
from treadmill_agent.config import Settings
from treadmill_agent.runner_dispositions import (
    handle_analysis,
    handle_architecture,
    handle_code,
    handle_documentation,
    handle_plan_doc,
    handle_review,
    handle_validation,
)
from treadmill_agent.runner_dispositions.architecture import (
    ArchitectVerdictParseError,
)
from treadmill_agent.runner_dispositions._context import DispositionContext
from treadmill_agent.runner_dispositions.plan_doc import PlanDocScopeError
from treadmill_agent.runner_dispositions.review import (
    MissingContextError,
    ReviewVerdict,
    _extract_json_block,
    _parse_review_envelope,
    _strip_json_block,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ctx(
    *,
    output_kind: str = "code",
    pr_number: int | None = None,
    role_id: str = "role-test",
    workflow_id: str = "wf-test",
    task_validations: list | None = None,
    trigger: str = "registered",
) -> WorkerContext:
    return WorkerContext(
        step_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        step_index=0,
        step_name="step",
        status="pending",
        task_id=str(uuid.uuid4()),
        plan_id=str(uuid.uuid4()),
        repo="t/r",
        title="Add a thing",
        description=None,
        plan_intent="goal",
        plan_doc_path=None,
        workflow_id=workflow_id,
        workflow_version=1,
        trigger=trigger,
        role=Role(
            id=role_id, model="m", system_prompt="p",
            output_kind=output_kind, skills=[], hooks=[],
        ),
        pr_number=pr_number,
        prior_steps=[],
        task_validations=task_validations or [],
    )


def _settings(repo_mode: str = "local") -> Settings:
    return Settings(
        api_url="http://fake",
        work_queue_url="http://sqs/q",
        events_topic_arn="arn",
        aws_endpoint_url=None,
        aws_region="us-east-1",
        repo_mode=repo_mode,
        bare_repos_dir="/tmp/bare",
        workspace_dir="/tmp/ws",
        exit_after_step=True,
        poll_wait_seconds=1,
        claude_credentials_path="/root/.claude/.credentials.json",
    )


def _disp_ctx(
    *,
    repo_dir: Path,
    output_kind: str = "code",
    summary: str = "did it",
    pr_number: int | None = None,
    role_id: str = "role-test",
    workflow_id: str = "wf-test",
    is_dry_run: bool = False,
    repo_mode: str = "local",
    task_validations: list | None = None,
    trigger: str = "registered",
) -> DispositionContext:
    return DispositionContext(
        ctx=_ctx(
            output_kind=output_kind,
            pr_number=pr_number,
            role_id=role_id,
            workflow_id=workflow_id,
            task_validations=task_validations,
            trigger=trigger,
        ),
        claude_result=CodeAuthorResult(summary=summary),
        repo_dir=repo_dir,
        branch="task/x-add-thing",
        settings=_settings(repo_mode=repo_mode),
        is_dry_run=is_dry_run,
    )


def _init_bare_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Init a bare + clone, return (bare_path, clone_path)."""
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True,
    )
    # Seed an initial commit.
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", str(seed)], check=True)
    (seed / "README.md").write_text("# r\n")
    for cmd in (
        ["git", "-C", str(seed), "config", "user.email", "t@t"],
        ["git", "-C", str(seed), "config", "user.name", "t"],
        ["git", "-C", str(seed), "add", "-A"],
        ["git", "-C", str(seed), "commit", "-m", "init"],
        ["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
        ["git", "-C", str(seed), "push", "origin", "main"],
    ):
        subprocess.run(cmd, check=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(clone), "checkout", "-b", "task/x-add-thing"],
        check=True,
    )
    return bare, clone


# ── code handler ─────────────────────────────────────────────────────────────


def test_code_handler_commits_pushes_and_returns_envelope(tmp_path: Path) -> None:
    """A diff in the working tree → code.handle stages, commits,
    pushes, and returns a ``StepOutput`` with the branch + commit_sha."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")
    ctx = _disp_ctx(repo_dir=clone)
    out = handle_code(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha
    branches = [a.value for a in out.artifacts if a.kind == "branch"]
    assert branches == ["task/x-add-thing"]


def test_code_handler_raises_on_empty_diff(tmp_path: Path) -> None:
    """Claude Code produced no changes → ``CodeAuthorError``. This
    is today's runner behavior preserved into the code handler for
    ``wf-author`` (the workflow that originates code changes)."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    # No file changes — diff is empty.
    ctx = _disp_ctx(repo_dir=clone, workflow_id="wf-author")
    with pytest.raises(claude_code.CodeAuthorError, match="no changes"):
        handle_code(ctx)


def test_code_handler_softens_empty_diff_for_wf_feedback(tmp_path: Path) -> None:
    """ADR-0012 documents ``responded-without-change`` as wf-feedback
    action's canonical empty-diff decision. The handler emits that
    rather than raising — failing would orphan the PR in
    changes_requested with no path forward (see the ADR-0023 smoke
    handoff for the live failure that motivated this)."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    ctx = _disp_ctx(repo_dir=clone, workflow_id="wf-feedback", pr_number=20)
    out = handle_code(ctx)
    assert out.decision == "responded-without-change"
    assert out.commit_sha is None
    assert out.artifacts == []
    assert out.payload == {"pr_number": 20}


def test_code_handler_softens_empty_diff_without_pr_number(tmp_path: Path) -> None:
    """The pr_number is propagated when present (for the downstream
    consumer / mergeability VIEW) but omitted cleanly when absent —
    no synthetic placeholder."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    ctx = _disp_ctx(repo_dir=clone, workflow_id="wf-feedback", pr_number=None)
    out = handle_code(ctx)
    assert out.decision == "responded-without-change"
    assert out.payload == {}


def test_code_handler_still_raises_for_wf_ci_fix_on_empty_diff(
    tmp_path: Path,
) -> None:
    """Empty-diff softening is deliberately limited to wf-feedback at
    v0 (per the module docstring). wf-ci-fix's empty-diff semantics
    need explicit role-prompt coupling (not-our-bug vs gave-up); until
    that lands, the strict raise is the safer default."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    ctx = _disp_ctx(repo_dir=clone, workflow_id="wf-ci-fix")
    with pytest.raises(claude_code.CodeAuthorError, match="no changes"):
        handle_code(ctx)


def test_code_handler_wf_author_opens_new_pr(tmp_path: Path) -> None:
    """wf-author creates a new PR when none exists. This is the primary
    workflow path — originating code changes and opening a PR."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")
    ctx = _disp_ctx(repo_dir=clone, workflow_id="wf-author")
    out = handle_code(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha
    # Local mode returns no URL
    pr_urls = [a.value for a in out.artifacts if a.kind == "pr_url"]
    assert len(pr_urls) == 0
    # Local mode returns no PR number
    assert out.payload.get("pr_number") is None


def test_code_handler_wf_feedback_noop_on_existing_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wf-feedback against an existing PR no-ops gh pr create — it detects
    the existing PR and returns its number without attempting to create."""
    from treadmill_agent import git as git_module

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "file.md").write_text("content\n")

    create_called = []

    def _fake_capture(cmd, **kwargs):
        # Simulate gh pr list returning an existing PR.
        if "pr" in cmd and "list" in cmd:
            return '[{"number": 42, "url": "https://github.com/owner/repo/pull/42"}]'
        # Ensure gh pr create is NOT called when PR already exists.
        if "pr" in cmd and "create" in cmd:
            create_called.append(True)
            raise AssertionError(
                "gh pr create should not be called when PR already exists"
            )
        return "dummy"

    monkeypatch.setattr(git_module, "_capture", _fake_capture)

    ctx = _disp_ctx(
        repo_dir=clone, workflow_id="wf-feedback", pr_number=42,
        summary="Addressed feedback", repo_mode="github",
    )
    out = handle_code(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha
    # The PR number should be in payload when wf-feedback has one.
    assert out.payload.get("pr_number") == 42
    # Verify gh pr create was never called.
    assert not create_called


def test_code_handler_wf_feedback_skips_merged_deleted_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wf-feedback against a merged+deleted branch logs a warning and returns
    success (with no pr_number) rather than raising."""
    from treadmill_agent import git as git_module

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "file.md").write_text("content\n")

    def _fake_capture(cmd, **kwargs):
        # PR doesn't exist (branch was deleted).
        if "pr" in cmd and "list" in cmd:
            return "[]"
        # PR create fails because the branch is gone.
        if "pr" in cmd and "create" in cmd:
            raise git_module.GitOpsError(
                "command failed: gh pr create\n"
                "stderr: pull request already exists, "
                "or the head branch was deleted"
            )
        return "dummy"

    monkeypatch.setattr(git_module, "_capture", _fake_capture)

    ctx = _disp_ctx(
        repo_dir=clone, workflow_id="wf-feedback", pr_number=None,
        summary="Response without PR",
    )
    out = handle_code(ctx)
    # Handler should succeed and return a commit.
    assert out.decision == "pushed"
    assert out.commit_sha
    # But no PR number because the branch is gone.
    assert "pr_number" not in out.payload


# ── Author-side validation (2026-05-14 learning) ──────────────────────────────


def test_code_handler_runs_passing_validation_and_pushes(tmp_path: Path) -> None:
    """Per the 2026-05-14 learning, deterministic validation scripts are
    run before pushing. A passing script → push proceeds normally."""
    from treadmill_agent.api_client import TaskValidationInfo

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")

    # Task has a passing validation script.
    ctx = _disp_ctx(repo_dir=clone, task_validations=[
            TaskValidationInfo(
                id="test-check",
                kind="deterministic",
                description="always passes",
                script="exit 0",
                prompt=None,
            )
        ])

    out = handle_code(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha
    branches = [a.value for a in out.artifacts if a.kind == "branch"]
    assert branches == ["task/x-add-thing"]


def test_code_handler_fails_on_failing_validation_script(tmp_path: Path) -> None:
    """A failing deterministic validation script → decision=fail, skip push."""
    from treadmill_agent.api_client import TaskValidationInfo

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")

    # Task has a failing validation script.
    ctx = _disp_ctx(repo_dir=clone, task_validations=[
            TaskValidationInfo(
                id="test-check",
                kind="deterministic",
                description="always fails",
                script="exit 1",
                prompt=None,
            )
        ])

    out = handle_code(ctx)
    assert out.decision == "fail"
    assert out.commit_sha is None
    assert out.artifacts == []
    # Validation result is captured in payload.
    assert "validation_results" in out.payload
    results = out.payload["validation_results"]
    assert len(results) == 1
    assert results[0]["check_id"] == "test-check"
    assert results[0]["verdict"] == "fail"


def test_code_handler_fails_on_first_failing_check_of_many(tmp_path: Path) -> None:
    """With multiple checks, the handler fails on the first failure."""
    from treadmill_agent.api_client import TaskValidationInfo

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")

    ctx = _disp_ctx(repo_dir=clone, task_validations=[
            TaskValidationInfo(
                id="check-1",
                kind="deterministic",
                description="passes",
                script="exit 0",
                prompt=None,
            ),
            TaskValidationInfo(
                id="check-2",
                kind="deterministic",
                description="fails",
                script="exit 1",
                prompt=None,
            ),
        ])

    out = handle_code(ctx)
    assert out.decision == "fail"
    assert out.commit_sha is None
    # Both checks ran, but the failure is captured.
    results = out.payload["validation_results"]
    assert len(results) == 2
    assert results[0]["verdict"] == "pass"
    assert results[1]["verdict"] == "fail"


def test_code_handler_ignores_llm_judge_checks_at_author_time(
    tmp_path: Path,
) -> None:
    """LLM-judge checks are deferred to wf-validate post-merge. Author
    time only runs deterministic checks."""
    from treadmill_agent.api_client import TaskValidationInfo

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")

    ctx = _disp_ctx(repo_dir=clone, task_validations=[
            TaskValidationInfo(
                id="llm-check",
                kind="llm-judge",
                description="linter",
                script=None,
                prompt="check the code",
            )
        ])

    # LLM-judge check is present but should not cause author-side failure.
    # It just gets skipped.
    out = handle_code(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha


def test_code_handler_with_empty_validations_list(tmp_path: Path) -> None:
    """Empty or missing validations list → normal push."""
    from treadmill_agent.api_client import TaskValidationInfo

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")

    ctx = _disp_ctx(repo_dir=clone, task_validations=[])

    out = handle_code(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha


def test_code_handler_captures_validation_stderr(tmp_path: Path) -> None:
    """Validation script stderr is captured in the failure output."""
    from treadmill_agent.api_client import TaskValidationInfo

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")

    ctx = _disp_ctx(repo_dir=clone, task_validations=[
            TaskValidationInfo(
                id="test-check",
                kind="deterministic",
                description="test with stderr",
                script="echo 'error message' >&2; exit 1",
                prompt=None,
            )
        ])

    out = handle_code(ctx)
    assert out.decision == "fail"
    results = out.payload["validation_results"]
    assert len(results) == 1
    # stderr excerpt is captured.
    assert "error message" in results[0]["log_excerpt"]


def test_code_handler_captures_rejected_diff_on_validation_fail(
    tmp_path: Path,
) -> None:
    """When author-side validation fails, the rejected diff is bundled
    into the StepOutput payload so a downstream architect can inspect
    the actual code change. (ADR-0048 follow-on, PR
    ``capture-rejected-diff-for-architect``.)"""
    from treadmill_agent.api_client import TaskValidationInfo

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello rejected world\n")

    ctx = _disp_ctx(repo_dir=clone, task_validations=[
            TaskValidationInfo(
                id="test-check",
                kind="deterministic",
                description="always fails",
                script="exit 1",
                prompt=None,
            )
        ])

    out = handle_code(ctx)
    assert out.decision == "fail"
    # The diff text the worker committed is in the payload.
    assert "rejected_diff" in out.payload
    diff_text = out.payload["rejected_diff"]
    assert diff_text  # non-empty
    # Unified-diff shape: ``+`` lines for the added file content.
    assert "NEW.md" in diff_text
    assert "hello rejected world" in diff_text
    # Small diff → not truncated.
    assert out.payload["rejected_diff_truncated"] is False


def test_code_handler_truncates_large_rejected_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diff larger than ``_REJECTED_DIFF_MAX_CHARS`` is truncated and
    flagged via ``rejected_diff_truncated: True``."""
    from treadmill_agent.api_client import TaskValidationInfo
    from treadmill_agent.runner_dispositions import code as code_mod

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("a\n")

    # Shrink the cap so we don't have to materialize 50K+ chars on disk.
    monkeypatch.setattr(code_mod, "_REJECTED_DIFF_MAX_CHARS", 20)

    ctx = _disp_ctx(repo_dir=clone, task_validations=[
            TaskValidationInfo(
                id="test-check",
                kind="deterministic",
                description="always fails",
                script="exit 1",
                prompt=None,
            )
        ])

    out = handle_code(ctx)
    assert out.decision == "fail"
    assert out.payload["rejected_diff_truncated"] is True
    assert len(out.payload["rejected_diff"]) == 20


def test_code_handler_survives_rejected_diff_capture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If diff capture itself fails (e.g., ``git show`` errors), the
    step still returns its validation-failure StepOutput; the diff
    enhancement is non-fatal."""
    from treadmill_agent.api_client import TaskValidationInfo
    from treadmill_agent.runner_dispositions import code as code_mod

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")

    def _boom(*a: Any, **kw: Any) -> tuple[str, bool]:
        raise git.GitOpsError("synthetic failure")

    monkeypatch.setattr(code_mod.git, "get_head_diff_text", _boom)

    ctx = _disp_ctx(repo_dir=clone, task_validations=[
            TaskValidationInfo(
                id="test-check",
                kind="deterministic",
                description="always fails",
                script="exit 1",
                prompt=None,
            )
        ])

    out = handle_code(ctx)
    assert out.decision == "fail"
    # validation_results still present — that's the load-bearing field.
    assert "validation_results" in out.payload
    assert len(out.payload["validation_results"]) == 1
    # Diff fields omitted on capture failure.
    assert "rejected_diff" not in out.payload
    assert "rejected_diff_truncated" not in out.payload


def test_code_handler_does_not_capture_rejected_diff_when_passing(
    tmp_path: Path,
) -> None:
    """When all validations pass, the push proceeds — the diff-capture
    code path is not reached and ``rejected_diff`` does not leak into
    the success payload."""
    from treadmill_agent.api_client import TaskValidationInfo

    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "NEW.md").write_text("hello\n")

    ctx = _disp_ctx(repo_dir=clone, task_validations=[
            TaskValidationInfo(
                id="test-check",
                kind="deterministic",
                description="always passes",
                script="exit 0",
                prompt=None,
            )
        ])

    out = handle_code(ctx)
    assert out.decision == "pushed"
    assert "rejected_diff" not in out.payload
    assert "rejected_diff_truncated" not in out.payload


# ── review handler ──────────────────────────────────────────────────────────


# ── ADR-0027: JSON envelope path ─────────────────────────────────────────────


def test_extract_json_block_picks_last_fence() -> None:
    """``_extract_json_block`` returns the LAST ```json fence so a
    drift-inducing earlier fence (e.g., example data the model
    rendered) doesn't shadow the terminal verdict block."""
    text = (
        "Earlier I might cite:\n"
        "```json\n"
        '{"example": "ignored"}\n'
        "```\n"
        "\nNow my actual verdict:\n"
        "```json\n"
        '{"verdict": "approve", "rationale": "looks good"}\n'
        "```\n"
    )
    block = _extract_json_block(text)
    assert block is not None
    assert '"verdict": "approve"' in block
    assert "example" not in block


def test_extract_json_block_returns_none_when_absent() -> None:
    assert _extract_json_block("no fence here, just prose") is None
    assert _extract_json_block("") is None


def test_extract_json_block_tolerates_mixed_case_lang_tag() -> None:
    """The fence lang tag is case-insensitive per ADR-0027 — JSON,
    Json, json5 all match. yaml does NOT."""
    for tag in ("json", "JSON", "Json", "json5"):
        text = f"prose\n```{tag}\n" + '{"v": 1}\n```\n'
        assert _extract_json_block(text) == '{"v": 1}'


def test_extract_json_block_rejects_non_json_fences() -> None:
    """A ```yaml block looks structurally similar but is not the
    JSON contract — the language-tag whitelist is the guard."""
    text = "```yaml\nverdict: approve\n```\n"
    assert _extract_json_block(text) is None


def test_strip_json_block_removes_only_last_fence() -> None:
    """``_strip_json_block`` removes only the last fence so earlier
    legitimate blocks survive — defensive for the model that emits
    example data plus a terminal verdict."""
    text = (
        "Example:\n"
        "```json\n"
        '{"example": "keep me"}\n'
        "```\n"
        "Verdict:\n"
        "```json\n"
        '{"verdict": "approve", "rationale": "ok"}\n'
        "```\n"
    )
    out = _strip_json_block(text)
    assert "keep me" in out
    assert "verdict" not in out  # the terminal block is gone
    assert "Verdict:" in out  # surrounding prose preserved


def test_strip_json_block_noop_when_no_fence() -> None:
    assert _strip_json_block("just prose") == "just prose"
    assert _strip_json_block("") == ""


def test_review_verdict_pydantic_rejects_unknown_verdict() -> None:
    """Closed value-set is the contract — Pydantic raises on
    anything outside approve / request_changes / comment."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ReviewVerdict.model_validate({"verdict": "lgtm", "rationale": "x"})


def test_review_verdict_pydantic_enforces_rationale_max_length() -> None:
    """Q27.b: max_length=4000 on rationale. 4001 chars rejects."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ReviewVerdict.model_validate({
            "verdict": "approve",
            "rationale": "x" * 4001,
        })
    # 4000 is fine.
    ok = ReviewVerdict.model_validate({
        "verdict": "approve",
        "rationale": "x" * 4000,
    })
    assert len(ok.rationale) == 4000


def test_parse_review_envelope_picks_from_json_fence_happy_path() -> None:
    """The primary parser path: a clean JSON fence returns
    ``(verdict, rationale)`` from the typed model."""
    text = (
        "Reviewed the diff.\n\n"
        "```json\n"
        '{"verdict": "request_changes", "rationale": "missing tests"}\n'
        "```\n"
    )
    verdict, rationale = _parse_review_envelope(text)
    assert verdict == "request_changes"
    assert rationale == "missing tests"


def test_parse_review_envelope_returns_safe_default_on_invalid_json() -> None:
    """When the JSON block is malformed (syntactic), the parser emits a
    warning and returns the safe default ``request_changes``."""
    text = (
        "Reviewed the diff.\n\n"
        "```json\n"
        '{"verdict": "approve", but this is not valid json\n'
        "```\n\n"
    )
    verdict, rationale = _parse_review_envelope(text)
    assert verdict == "request_changes"
    assert rationale is None


def test_parse_review_envelope_returns_safe_default_on_invalid_verdict_value() -> None:
    """When the JSON block parses but the verdict is outside the
    closed value-set, the envelope falls through to the safe default."""
    text = (
        "```json\n"
        '{"verdict": "lgtm", "rationale": "looks good"}\n'
        "```\n"
    )
    verdict, rationale = _parse_review_envelope(text)
    assert verdict == "request_changes"
    assert rationale is None


def test_parse_review_envelope_logs_warning_on_json_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Q27.d: parse failures emit a structured ``review.json_parse_failed``
    warning — the drift signal that the model has stopped honoring the
    JSON envelope contract."""
    import logging
    caplog.set_level(logging.WARNING, logger="treadmill.agent.review")
    text = (
        "```json\n"
        '{"verdict": "lgtm", "rationale": "..."}\n'  # invalid verdict
        "```\n"
    )
    _parse_review_envelope(text)
    assert any(
        "review.json_parse_failed" in rec.message
        for rec in caplog.records
    )


def test_parse_review_envelope_safe_default_when_both_paths_fail() -> None:
    """No JSON, no VERDICT line → safe default ``request_changes``.
    Comment was retired 2026-05-15; the conservative fallback is now
    request_changes (forces a productive next step, never silently
    approves)."""
    verdict, rationale = _parse_review_envelope("Just prose, no marker at all.")
    assert verdict == "request_changes"
    assert rationale is None


def test_parse_review_envelope_legacy_comment_marker_falls_to_default() -> None:
    """A stale ``VERDICT: comment`` line (e.g. from a pre-2026-05-15
    role prompt cached somewhere) is not a valid JSON envelope, so the
    envelope falls through to the request_changes safe default."""
    text = "Some notes.\nVERDICT: comment\n"
    verdict, rationale = _parse_review_envelope(text)
    assert verdict == "request_changes"
    assert rationale is None


@pytest.fixture
def _stub_head_sha(monkeypatch: pytest.MonkeyPatch) -> str:
    """Stub ``git.head_sha`` for review-handler tests that use a
    bare ``tmp_path`` (not a real git repo). Returns a fixed sha so
    assertions on ``out.commit_sha`` are deterministic."""
    sha = "deadbeef00000000000000000000000000000000"
    monkeypatch.setattr(git, "head_sha", lambda _repo_dir: sha)
    return sha


def test_review_handler_synthesizes_structured_body_from_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_head_sha: str,
) -> None:
    """ADR-0036: the disposition synthesizes a structured body from the
    ReviewVerdict envelope rather than passing the model's free-form prose.
    The body is generated from a template with header + rationale + issues
    (if request_changes)."""
    captured: list[str] = []
    monkeypatch.setattr(
        gh, "pr_comment",
        lambda pr_number, *, body, cwd=None: captured.append(body),
    )

    summary = (
        "Diff has correctness issues with the merge-key logic.\n\n"
        "```json\n"
        '{"verdict": "request_changes", "rationale": "fix the merge key"}\n'
        "```\n"
    )
    ctx = _disp_ctx(
        repo_dir=tmp_path, output_kind="review", pr_number=42,
        summary=summary,
    )
    out = handle_review(ctx)
    assert len(captured) == 1
    # The JSON fence is gone from the body.
    assert "```json" not in captured[0]
    # The synthesized body has the structured header + rationale.
    assert "## Treadmill review verdict: request changes" in captured[0]
    assert "fix the merge key" in captured[0]
    # Issues section is generated from the single-sentence rationale.
    assert "## Issues" in captured[0]
    assert "- fix the merge key" in captured[0]
    # The verdict + rationale travel via the StepOutput envelope.
    assert out.decision == "changes_requested"
    assert out.payload["verdict"] == "request_changes"
    assert out.payload["rationale"] == "fix the merge key"


def test_review_handler_dry_run_still_parses_per_q27d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_head_sha: str,
) -> None:
    """Q27.d resolution: the parser runs unconditionally even on
    dry-run, so the drift warning surfaces in tests + dev exploration.
    Only ``gh pr comment`` itself is dry-run-gated."""
    def _fail(*_args, **_kwargs):
        raise AssertionError("gh.pr_comment should not be called in dry-run")

    monkeypatch.setattr(gh, "pr_comment", _fail)
    monkeypatch.setattr(gh, "pr_review", _fail)

    summary = (
        "```json\n"
        '{"verdict": "approve", "rationale": "lgtm"}\n'
        "```\n"
    )
    ctx = _disp_ctx(
        repo_dir=tmp_path, output_kind="review", pr_number=42,
        summary=summary, is_dry_run=True,
    )
    out = handle_review(ctx)
    # Parser fired even in dry-run, so the envelope has the rationale.
    assert out.payload["verdict"] == "approve"
    assert out.payload["rationale"] == "lgtm"
    assert out.decision == "approved"


def test_review_handler_raises_without_pr_number(tmp_path: Path) -> None:
    """A review-kind step against a task that hasn't opened a PR yet
    is a config error — raise loudly so the operator sees it as a
    clean step.failed."""
    ctx = _disp_ctx(
        repo_dir=tmp_path, output_kind="review", pr_number=None,
    )
    with pytest.raises(MissingContextError, match="pr_number"):
        handle_review(ctx)


def test_review_handler_invokes_gh_pr_comment_with_verdict_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_head_sha: str,
) -> None:
    """The review handler shells out to ``gh.pr_comment`` (NOT
    ``gh.pr_review`` — GitHub blocks same-author reviews; task #108
    path 1) with a body that prepends a human-readable verdict header
    so PR-page readers see the verdict above the prose. The decision
    on the envelope still maps to ADR-0012's wf-review value set."""
    calls: list[dict[str, Any]] = []

    def _fake_comment(pr_number, *, body, cwd=None):
        calls.append({"pr": pr_number, "body": body})

    def _fail_review(*_args, **_kwargs):
        raise AssertionError("gh.pr_review must not be called (#108 path 1)")

    monkeypatch.setattr(gh, "pr_comment", _fake_comment)
    monkeypatch.setattr(gh, "pr_review", _fail_review)

    ctx = _disp_ctx(
        repo_dir=tmp_path, output_kind="review", pr_number=42,
        summary=(
            "Reviewed the diff carefully.\n\n"
            "```json\n"
            '{"verdict": "approve", "rationale": "LGTM"}\n'
            "```\n"
        ),
    )
    out = handle_review(ctx)
    assert len(calls) == 1
    assert calls[0]["pr"] == 42
    assert calls[0]["body"].startswith("## Treadmill review verdict: approve\n\n")
    assert "LGTM" in calls[0]["body"]
    # ADR-0012 mapping: approve → approved.
    assert out.decision == "approved"
    review_artifacts = [a for a in out.artifacts if a.kind == "pr_review"]
    assert len(review_artifacts) == 1
    assert review_artifacts[0].value == "approve"
    assert out.payload["pr_number"] == 42
    assert out.payload["verdict"] == "approve"


def test_review_handler_request_changes_header_uses_human_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_head_sha: str,
) -> None:
    """``request_changes`` reads naturally in prose as
    ``request changes`` — the header verb is the human-facing form,
    not the snake_case value-set member."""
    captured: list[str] = []
    monkeypatch.setattr(
        gh, "pr_comment",
        lambda pr_number, *, body, cwd=None: captured.append(body),
    )
    monkeypatch.setattr(gh, "pr_review", lambda *a, **kw: None)
    ctx = _disp_ctx(
        repo_dir=tmp_path, output_kind="review", pr_number=11,
        summary=(
            "Diff has correctness issues.\n\n"
            "```json\n"
            '{"verdict": "request_changes", "rationale": "Diff has correctness issues"}\n'
            "```\n"
        ),
    )
    handle_review(ctx)
    assert captured[0].startswith("## Treadmill review verdict: request changes\n\n")


def test_review_handler_skips_gh_in_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_head_sha: str,
) -> None:
    """Dry-run path doesn't touch the gh CLI — the envelope still
    reflects the parsed verdict so tests can exercise the marker
    convention without a live GitHub."""
    def _fail(*_args, **_kwargs):
        raise AssertionError("gh.pr_comment should not be called in dry-run")

    monkeypatch.setattr(gh, "pr_comment", _fail)
    monkeypatch.setattr(gh, "pr_review", _fail)
    ctx = _disp_ctx(
        repo_dir=tmp_path, output_kind="review", pr_number=42,
        summary=(
            "ok\n\n"
            "```json\n"
            '{"verdict": "request_changes", "rationale": "ok"}\n'
            "```\n"
        ),
        is_dry_run=True,
    )
    out = handle_review(ctx)
    assert out.decision == "changes_requested"


# ── analysis handler ────────────────────────────────────────────────────────


def test_analysis_handler_emits_artifact_with_summary(tmp_path: Path) -> None:
    """The handler returns a ``StepOutput`` with the summary as an
    ``Artifact(kind="analysis", ...)``. No git side effects."""
    ctx = _disp_ctx(
        repo_dir=tmp_path, output_kind="analysis",
        summary="Classified comment into request_changes.",
    )
    out = handle_analysis(ctx)
    analysis_artifacts = [a for a in out.artifacts if a.kind == "analysis"]
    assert len(analysis_artifacts) == 1
    assert (
        analysis_artifacts[0].value
        == "Classified comment into request_changes."
    )
    # Decision is the analyzer→action contract default.
    assert out.decision == "plan-ready"
    # No commit, no PR.
    assert out.commit_sha is None
    pr_urls = [a for a in out.artifacts if a.kind == "pr_url"]
    assert pr_urls == []


# ── plan_doc handler ────────────────────────────────────────────────────────


def test_plan_doc_handler_accepts_diff_under_docs_plans(tmp_path: Path) -> None:
    """A diff confined to ``docs/plans/`` passes the confinement check
    and falls through to the code handler's commit/push path."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "docs" / "plans").mkdir(parents=True)
    (clone / "docs" / "plans" / "2026-05-12-x.md").write_text("# plan\n")
    ctx = _disp_ctx(
        repo_dir=clone, output_kind="plan_doc", role_id="role-doc-author",
    )
    out = handle_plan_doc(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha


def test_plan_doc_handler_rejects_diff_outside_docs_plans(tmp_path: Path) -> None:
    """A diff that touches files outside ``docs/plans/`` is a constraint
    violation — raise ``PlanDocScopeError`` (sub-class of CodeAuthorError
    so the runner's exception layer captures it cleanly)."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "src.py").write_text("# wrong place\n")
    ctx = _disp_ctx(
        repo_dir=clone, output_kind="plan_doc", role_id="role-doc-author",
    )
    with pytest.raises(PlanDocScopeError, match="docs/plans/"):
        handle_plan_doc(ctx)


# ── validation handler ──────────────────────────────────────────────────────


def test_validation_handler_aggregates_worst_wins_with_blocking_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation handler aggregates with worst-wins, but ONLY blocking
    severity checks count toward the decision. Advisory failures do not
    flip the aggregate to fail."""
    # Stub subprocess calls for gh pr diff
    def _fake_run(cmd, **kwargs):
        result = MagicMock()
        if "--name-only" in cmd:
            result.stdout = "src/main.py\ntest_main.py\n"
        elif "rev-parse" in cmd and "HEAD" in cmd:
            result.stdout = "abc123def456\n"
        else:
            result.stdout = "diff content"
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # Stub gh.pr_comment to verify it gets called
    called_with = {}

    def _fake_comment(pr_number, **kwargs):
        called_with["pr"] = pr_number
        called_with["body"] = kwargs.get("body")

    monkeypatch.setattr(gh, "pr_comment", _fake_comment)

    # Stub validation_runtime to return deterministic results
    def _fake_deterministic(check, repo_dir, timeout_seconds, pr_number=None):
        from treadmill_agent import validation_runtime

        # blocking:pass, warning:fail, advisory:fail
        verdicts = {
            "blocking-check": "pass",
            "warning-check": "fail",
            "advisory-check": "fail",
        }
        return validation_runtime.CheckResult(
            check_id=check.id,
            kind="deterministic",
            severity=check.severity,
            verdict=verdicts.get(check.id, "pass"),
            rationale=f"{check.id} rationale",
            log_excerpt="",
        )

    monkeypatch.setattr(
        "treadmill_agent.validation_runtime.run_deterministic", _fake_deterministic
    )

    # Create synthetic checks
    class FakeCheck:
        def __init__(self, check_id, severity):
            self.id = check_id
            self.severity = severity
            self.kind = "deterministic"
            self.description = ""

    checks = [
        FakeCheck("blocking-check", "blocking"),
        FakeCheck("warning-check", "warning"),
        FakeCheck("advisory-check", "advisory"),
    ]

    # Stub _load_checks to return our synthetic checks
    def _fake_load_checks(ctx):
        return checks

    import treadmill_agent.runner_dispositions.validation as val_module

    monkeypatch.setattr(val_module, "_load_checks", _fake_load_checks)

    # Call the handler
    ctx = _disp_ctx(
        repo_dir=tmp_path, pr_number=42, workflow_id="wf-validate"
    )
    out = handle_validation(ctx)

    # Decision should be 'pass' because the only blocking check passed.
    # The warning and advisory failures don't flip the aggregate.
    assert out.decision == "pass"
    assert out.commit_sha == "abc123def456"
    assert len(out.payload["checks"]) == 3
    assert called_with["pr"] == 42
    assert "Validation Results" in called_with["body"]


def test_validation_handler_fails_on_blocking_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a blocking severity check fails, the aggregate flips to fail."""

    def _fake_run(cmd, **kwargs):
        result = MagicMock()
        if "rev-parse" in cmd and "HEAD" in cmd:
            result.stdout = "abc123def456\n"
        else:
            result.stdout = "src/main.py\n"
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(gh, "pr_comment", lambda *a, **kw: None)

    from treadmill_agent import validation_runtime

    def _fake_deterministic(check, repo_dir, timeout_seconds, pr_number=None):
        return validation_runtime.CheckResult(
            check_id=check.id,
            kind="deterministic",
            severity=check.severity,
            verdict="fail",
            rationale="failed",
            log_excerpt="",
        )

    monkeypatch.setattr(
        "treadmill_agent.validation_runtime.run_deterministic", _fake_deterministic
    )

    class FakeCheck:
        def __init__(self, check_id, severity):
            self.id = check_id
            self.severity = severity
            self.kind = "deterministic"
            self.description = ""

    checks = [FakeCheck("block1", "blocking")]

    import treadmill_agent.runner_dispositions.validation as val_module

    monkeypatch.setattr(val_module, "_load_checks", lambda ctx: checks)

    ctx = _disp_ctx(repo_dir=tmp_path, pr_number=42, workflow_id="wf-validate")
    out = handle_validation(ctx)
    assert out.decision == "fail"


def test_validation_handler_passes_on_blocking_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per ADR-0039, when only blocking errors are present, the aggregate
    flips to pass. Errors indicate the validator failed, not the code."""

    def _fake_run(cmd, **kwargs):
        result = MagicMock()
        if "rev-parse" in cmd and "HEAD" in cmd:
            result.stdout = "abc123def456\n"
        else:
            result.stdout = "src/main.py\n"
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(gh, "pr_comment", lambda *a, **kw: None)

    from treadmill_agent import validation_runtime

    def _fake_deterministic(check, repo_dir, timeout_seconds, pr_number=None):
        return validation_runtime.CheckResult(
            check_id=check.id,
            kind="deterministic",
            severity=check.severity,
            verdict="error",
            rationale="Judge timeout",
            log_excerpt="",
        )

    monkeypatch.setattr(
        "treadmill_agent.validation_runtime.run_deterministic", _fake_deterministic
    )

    class FakeCheck:
        def __init__(self, check_id, severity):
            self.id = check_id
            self.severity = severity
            self.kind = "deterministic"
            self.description = ""

    checks = [FakeCheck("block1", "blocking")]

    import treadmill_agent.runner_dispositions.validation as val_module

    monkeypatch.setattr(val_module, "_load_checks", lambda ctx: checks)

    ctx = _disp_ctx(repo_dir=tmp_path, pr_number=42, workflow_id="wf-validate")
    out = handle_validation(ctx)
    assert out.decision == "pass"


def test_validation_handler_raises_without_pr_number(tmp_path: Path) -> None:
    """The validation handler requires pr_number; absent context raises."""
    ctx = _disp_ctx(repo_dir=tmp_path, pr_number=None, workflow_id="wf-validate")
    with pytest.raises(ValueError, match="pr_number"):
        handle_validation(ctx)


def test_validation_handler_dry_run_skips_gh_pr_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In dry-run, the handler skips posting the comment but still returns
    the full envelope."""

    def _fail(*args, **kwargs):
        raise AssertionError("gh.pr_comment should not be called in dry-run")

    monkeypatch.setattr(gh, "pr_comment", _fail)

    def _fake_run(cmd, **kwargs):
        result = MagicMock()
        if "rev-parse" in cmd and "HEAD" in cmd:
            result.stdout = "abc123def456\n"
        else:
            result.stdout = "src/main.py\n"
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", _fake_run)

    from treadmill_agent import validation_runtime

    def _fake_deterministic(check, repo_dir, timeout_seconds, pr_number=None):
        return validation_runtime.CheckResult(
            check_id=check.id,
            kind="deterministic",
            severity="warning",
            verdict="pass",
            rationale="ok",
            log_excerpt="",
        )

    monkeypatch.setattr(
        "treadmill_agent.validation_runtime.run_deterministic", _fake_deterministic
    )

    class FakeCheck:
        def __init__(self, check_id):
            self.id = check_id
            self.severity = "warning"
            self.kind = "deterministic"
            self.description = ""

    checks = [FakeCheck("check1")]

    import treadmill_agent.runner_dispositions.validation as val_module

    monkeypatch.setattr(val_module, "_load_checks", lambda ctx: checks)

    ctx = _disp_ctx(
        repo_dir=tmp_path, pr_number=42, workflow_id="wf-validate", is_dry_run=True
    )
    out = handle_validation(ctx)
    assert out.decision == "pass"
    assert out.summary is not None


def test_validation_picks_up_new_rules_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule engine loads rules fresh on every invocation — no module-level
    cache. A rule file written between two _load_applicable_rules calls
    is visible to the second call without any restart (per ADR-0030)."""
    import treadmill_agent.runner_dispositions.validation as val_module

    def _fake_run(cmd, **kwargs):
        result = MagicMock()
        if "--name-only" in cmd:
            result.stdout = "src/main.py\n"
        elif "rev-parse" in cmd and "HEAD" in cmd:
            result.stdout = "abc123def456\n"
        else:
            result.stdout = ""
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # Set up a rules dir with one initial rule (no applies_to → universal).
    rules_dir = tmp_path / "docs" / "knowledge-base" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "rule-alpha.yaml").write_text(
        "name: rule-alpha\n"
        "description: first rule\n"
        "checks:\n"
        "  - id: alpha-check\n"
        "    type: deterministic\n"
        "    severity: advisory\n"
        "    script: exit 0\n"
    )

    ctx = _disp_ctx(repo_dir=tmp_path, pr_number=42, workflow_id="wf-validate")

    first_checks = val_module._load_applicable_rules(ctx)
    first_ids = {c.id for c in first_checks}
    assert "alpha-check" in first_ids
    assert "beta-check" not in first_ids

    # Crystallization lands a second rule mid-session (no restart).
    (rules_dir / "rule-beta.yaml").write_text(
        "name: rule-beta\n"
        "description: freshly crystallized rule\n"
        "checks:\n"
        "  - id: beta-check\n"
        "    type: deterministic\n"
        "    severity: advisory\n"
        "    script: exit 0\n"
    )

    second_checks = val_module._load_applicable_rules(ctx)
    second_ids = {c.id for c in second_checks}
    assert "alpha-check" in second_ids
    assert "beta-check" in second_ids, (
        "rule engine cached the corpus — new rules not visible without restart"
    )


# ── runner-level dispatch ────────────────────────────────────────────────────


def test_runner_dispatch_table_covers_all_five_kinds() -> None:
    """The dispatch table has exactly the five ADR-0022 kinds (code,
    review, analysis, plan_doc, documentation).  Per ADR-0029,
    validation dispatches by workflow_id (not in the output_kind table),
    so it's not counted here.  This test is the tripwire for new kinds."""
    from treadmill_agent.runner import DISPOSITIONS

    assert set(DISPOSITIONS) == {
        "code", "review", "analysis", "plan_doc", "documentation"
    }


def test_runner_dispatch_unknown_kind_raises_at_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When a role declares an output_kind that's not in the table,
    the worker raises ``UnknownOutputKindError`` so the operator
    sees a clean step.failed naming the offending kind."""
    from treadmill_agent import runner
    from treadmill_agent.runner import UnknownOutputKindError

    bare_repos_dir = tmp_path / "bare"
    bare_repos_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    # Seed a bare repo so the clone step succeeds.
    from tests.conftest import init_bare_repo  # type: ignore

    init_bare_repo(bare_repos_dir, "owner/test-repo")
    monkeypatch.setenv("TREADMILL_AGENT_DRY_RUN", "1")
    ctx = _ctx(output_kind="something_unknown")
    # Replace the ``repo`` so the bare-repo seeding is found.
    ctx = WorkerContext(**{**ctx.__dict__, "repo": "owner/test-repo"})
    settings = Settings(
        api_url="http://fake", work_queue_url="http://sqs/q",
        events_topic_arn="arn", aws_endpoint_url=None, aws_region="us-east-1",
        repo_mode="local", bare_repos_dir=str(bare_repos_dir),
        workspace_dir=str(workspace_dir), exit_after_step=True,
        poll_wait_seconds=1,
        claude_credentials_path="/root/.claude/.credentials.json",
    )
    with pytest.raises(UnknownOutputKindError, match="something_unknown"):
        runner._execute(ctx, settings)


# ── documentation handler ────────────────────────────────────────────────────


@pytest.mark.parametrize("gap_class", ["A", "B"])
def test_documentation_handler_class_ab_no_escalation(
    gap_class: str, tmp_path: Path,
) -> None:
    """Class A/B summaries: handler commits, pushes, opens PR.

    No learning file is written and no ``escalate`` key appears in the
    payload — there is no JSON envelope with ``gap_class`` in the summary.
    """
    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "docs" / "adrs").mkdir(parents=True, exist_ok=True)
    (clone / "docs" / "adrs" / "0001-amended.md").write_text("# ADR-0001 (amended)\n")
    summary = f"Amended ADR-0001 diagram to reflect current reality. Gap class: {gap_class}."
    ctx = _disp_ctx(
        repo_dir=clone,
        output_kind="documentation",
        summary=summary,
        workflow_id="wf-doc-amend",
    )
    out = handle_documentation(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha
    branches = [a.value for a in out.artifacts if a.kind == "branch"]
    assert branches == ["task/x-add-thing"]
    assert "escalate" not in out.payload
    # No gap learning file should exist.
    learning_dir = clone / "docs" / "learnings"
    if learning_dir.exists():
        assert list(learning_dir.glob("*-gap.md")) == []


def test_documentation_handler_class_c_writes_learning_and_escalates(
    tmp_path: Path,
) -> None:
    """Class C gap: learning file committed alongside amended doc,
    ``escalate`` key in payload points at ``wf-architecture-resolve``.
    """
    _bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "docs" / "adrs").mkdir(parents=True, exist_ok=True)
    (clone / "docs" / "adrs" / "0010-amended.md").write_text("# ADR-0010 (amended)\n")
    summary = (
        "Amended ADR-0010 sequence diagram. Detected a sub-optimality gap.\n\n"
        "```json\n"
        '{"gap_class": "C", "gap_slug": "async-violation", '
        '"gap_summary": "Current code violates async idempotency contract."}\n'
        "```\n"
    )
    ctx = _disp_ctx(
        repo_dir=clone,
        output_kind="documentation",
        summary=summary,
        workflow_id="wf-doc-amend",
    )
    out = handle_documentation(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha
    # escalate payload present and correct.
    assert "escalate" in out.payload
    assert out.payload["escalate"]["workflow_id"] == "wf-architecture-resolve"
    assert out.payload["escalate"]["gap_slug"] == "async-violation"
    assert out.payload["escalate"]["task_id"] == ctx.ctx.task_id
    # Learning file was written into the repo (and committed).
    learning_files = list(
        (clone / "docs" / "learnings").glob("*-async-violation-gap.md")
    )
    assert len(learning_files) == 1
    content = learning_files[0].read_text()
    assert "**Class:** C" in content
    assert "async idempotency" in content


def test_documentation_handler_raises_on_empty_diff(tmp_path: Path) -> None:
    """Empty diff → ``CodeAuthorError``; the documentarian produced no amendments."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    ctx = _disp_ctx(
        repo_dir=clone, output_kind="documentation", workflow_id="wf-doc-amend",
    )
    with pytest.raises(claude_code.CodeAuthorError, match="no changes"):
        handle_documentation(ctx)


# ── architecture handler (ADR-0032 wf-architecture-resolve) ────────────────────


def _arch_ctx(
    tmp_path: Path,
    summary: str,
    *,
    role_id: str = "role-architect",
    trigger: str = "registered",
    empty_branch: bool = False,
) -> DispositionContext:
    """Build a DispositionContext for the architect handler.

    Architect uses output_kind=analysis (per ADR-0032) but routes to
    handle_architecture via role.id branch in runner.py — for direct
    handler tests we just build a ctx with role-architect and a summary
    carrying the verdict envelope.

    Seeds a dummy commit on the task branch by default so the disposition's
    empty-diff safety check (forces accept-as-is → amend when no commits
    exist against origin/main) doesn't fire on every test. Pass
    ``empty_branch=True`` to skip the seed when testing the safety check
    itself.
    """
    _bare, clone = _init_bare_and_clone(tmp_path)
    if not empty_branch:
        (clone / "WORK.md").write_text("authored work\n")
        for cmd in (
            ["git", "-C", str(clone), "add", "-A"],
            ["git", "-C", str(clone), "commit", "-m", "task work"],
        ):
            subprocess.run(cmd, check=True, capture_output=True)
    ctx = _disp_ctx(
        repo_dir=clone,
        output_kind="analysis",
        role_id=role_id,
        workflow_id="wf-architecture-resolve",
        summary=summary,
        trigger=trigger,
    )
    return ctx


def test_architecture_handler_amend_verdict_routes_to_wf_plan(
    tmp_path: Path,
) -> None:
    """``amend`` verdict → payload.dispatch.workflow_id == 'wf-plan'."""
    summary = (
        "I reviewed the gap.\n\n"
        '```json\n'
        '{"verdict": "amend", "reasoning": "Code violates async '
        'idempotency standard; ADR intent is right.", '
        '"target_artifact": "services/api/treadmill_api/coordination/'
        'consumer.py", "remediation_summary": "Wrap _handle_step in an '
        'idempotency guard keyed on event_id."}\n'
        '```'
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "amend"
    assert out.commit_sha is None
    assert out.payload["dispatch"]["workflow_id"] == "wf-plan"
    assert out.payload["dispatch"]["task_id"] == ctx.ctx.task_id
    assert "Wrap _handle_step" in out.payload["dispatch"]["remediation_summary"]


def test_architecture_handler_supersede_verdict_routes_to_api_trigger(
    tmp_path: Path,
) -> None:
    """``supersede`` per ADR-0048 is repurposed: the disposition emits
    ``workflow_id=None`` + ``rewritten_description`` in the dispatch
    payload. The API-side
    ``maybe_dispatch_supersede_on_architect_verdict`` trigger handles
    the close-PR + create-child-task + dispatch-fresh-wf-author
    sequence — the disposition just surfaces the architect's rewritten
    text."""
    summary = (
        '```json\n'
        '{"verdict": "supersede", "reasoning": "Plan named the wrong '
        'file paths.", '
        '"target_artifact": "docs/plans/0010-branch-conventions.md", '
        '"rewritten_description": "Write services/api/treadmill_api/'
        'foo.py with function bar() that returns the new shape."}\n'
        '```'
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "supersede"
    assert out.payload["dispatch"]["workflow_id"] is None
    assert out.payload["dispatch"]["intent"] == "supersede-rewrite-task"
    assert "services/api" in out.payload["dispatch"]["rewritten_description"]
    # Also surfaced at top level for downstream readers.
    assert "services/api" in out.payload["rewritten_description"]


def test_architecture_handler_supersede_without_rewrite_raises(
    tmp_path: Path,
) -> None:
    """Per ADR-0048, ``verdict='supersede'`` without a non-empty
    ``rewritten_description`` is a parse failure. The disposition
    forbids the worker from emitting an empty-rewrite supersede so the
    step fails fast rather than silently dispatching an empty child
    task description."""
    summary = (
        '```json\n'
        '{"verdict": "supersede", "reasoning": "Plan is wrong", '
        '"target_artifact": "docs/plans/x.md"}\n'
        '```'
    )
    ctx = _arch_ctx(tmp_path, summary)
    with pytest.raises(ArchitectVerdictParseError) as exc_info:
        handle_architecture(ctx)
    assert "rewritten_description" in str(exc_info.value)


def test_architecture_handler_accept_as_is_emits_pr_comment(
    tmp_path: Path,
) -> None:
    """``accept-as-is`` → wf-doc-amend (Pitfalls) + pr_comment payload."""
    summary = (
        '```json\n'
        '{"verdict": "accept-as-is", "reasoning": "Tradeoff is acceptable '
        'for v1; capture in Pitfalls.", '
        '"target_artifact": "workers/agent/AGENT.md"}\n'
        '```'
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "accept-as-is"
    assert out.payload["dispatch"]["workflow_id"] == "wf-doc-amend"
    assert out.payload["dispatch"]["intent"] == "append-pitfall"
    # PR comment for operator confirmation per ADR-0033.
    assert "pr_comment" in out.payload
    assert out.payload["pr_comment"]["signal"] == "accept-as-is"


def test_architecture_handler_accept_as_is_on_deadlock_emits_review_override(
    tmp_path: Path,
) -> None:
    """ADR-0038: when the architect was dispatched with
    trigger=self:wf-feedback-deadlock, ``accept-as-is`` flips the
    dispatch payload from wf-doc-amend (the ADR-0032 Class C path) to a
    no-dispatch + review_override marker. The consumer projects this
    marker to a ``review.override`` Event row that the mergeability VIEW
    reads as ``review_decision='approved'``."""
    summary = (
        '```json\n'
        '{"verdict": "accept-as-is", "reasoning": "Reviewer was wrong; '
        'work is fine.", '
        '"target_artifact": "services/api/treadmill_api/coordination/'
        'consumer.py"}\n'
        '```'
    )
    ctx = _arch_ctx(tmp_path, summary, trigger="self:wf-feedback-deadlock")
    out = handle_architecture(ctx)
    assert out.decision == "accept-as-is"
    # No wf-doc-amend dispatch — the deadlock variant has no pitfall to
    # append; ``accept-as-is`` here means "reviewer was wrong" not
    # "the gap is acceptable, learn from it."
    assert out.payload["dispatch"]["workflow_id"] is None
    assert out.payload["dispatch"]["review_override"] is True
    assert out.payload["dispatch"]["task_id"] == ctx.ctx.task_id


def test_architecture_handler_raises_on_empty_summary(
    tmp_path: Path,
) -> None:
    """Empty / blank summary raises ``ArchitectVerdictParseError`` — no
    signal at all to parse. Surfaces as a step.failure that wf-feedback
    can re-run with an envelope reminder."""
    ctx = _arch_ctx(tmp_path, "")
    with pytest.raises(ArchitectVerdictParseError):
        handle_architecture(ctx)


def test_architecture_handler_raises_on_unrecognized_prose(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Post-ADR-0048, prose with no recognized verdict cue is a hard
    failure — the prior ``uncertain`` catch-all is gone. The retry
    helper is monkeypatched to a no-op so the cue path is exercised
    cleanly without making a real Claude call."""
    from treadmill_agent.runner_dispositions import architecture
    monkeypatch.setattr(architecture, "_try_structured_retry", lambda *a, **kw: None)
    ctx = _arch_ctx(tmp_path, "I thought about this for a while but didn't decide.")
    with pytest.raises(ArchitectVerdictParseError):
        handle_architecture(ctx)


def test_architecture_handler_structured_retry_yields_clean_verdict(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The structured-output retry is the highest-fidelity fallback when
    strict JSON parsing fails. Mock the retry helper to return a valid
    envelope and confirm the disposition uses that verdict instead of
    falling through to the prose-cue path or the hard-fail at the end."""
    from treadmill_agent.runner_dispositions import architecture

    def _fake_retry(summary: str, model: str, log_context: Any = None) -> dict[str, Any]:
        return {
            "verdict": "amend",
            "reasoning": "extracted via structured-output retry",
            "target_artifact": "services/api/X.py",
            "remediation_summary": "wire X to Y",
            "parsed_via_retry": True,
        }

    monkeypatch.setattr(architecture, "_try_structured_retry", _fake_retry)
    # Prose that has no cue match — proves the retry path is what
    # produces the verdict.
    ctx = _arch_ctx(tmp_path, "Some prose without any specific verdict cue.")
    out = handle_architecture(ctx)
    assert out.decision == "amend"
    assert out.payload.get("parsed_via_retry") is True
    assert out.payload.get("parsed_from_prose") is None


def test_architecture_handler_prose_fallback_extracts_accept_as_is(
    tmp_path: Path,
) -> None:
    """When the architect produces a clear prose verdict but omits the
    JSON envelope (observed 2026-05-15 on sonnet — see PR for the
    productivity-bug context), the disposition falls back to scanning
    prose for verdict cues. ``accept-as-is`` cues include phrases like
    'implementation is already complete' / 'no issues found' / 'all task
    requirements are implemented'. Task progresses with the synthesized
    verdict rather than dead-ending."""
    summary = (
        "I reviewed the diff carefully against the task spec.\n\n"
        "The implementation is already complete. The recent commit "
        "907b9c2 has delivered everything the task requires."
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "accept-as-is"
    assert out.payload["verdict"] == "accept-as-is"
    assert out.payload.get("parsed_from_prose") is True


def test_architecture_handler_forces_amend_on_empty_diff_branch(
    tmp_path: Path,
) -> None:
    """ADR-0032/ADR-0038 safety net: when the architect verdicts
    ``accept-as-is`` but the task branch has NO commits against
    origin/main (the worker's wf-author failed pre-push, leaving an
    empty workspace), force the verdict to ``amend``. accept-as-is
    fires ``review.override`` which is meaningless if no PR exists.
    The amend verdict re-engages wf-feedback (per PR #113) to author
    the missing work."""
    summary = (
        '```json\n{"verdict": "accept-as-is", "reasoning": "looks fine to '
        'me", "target_artifact": "services/api/X.py"}\n```'
    )
    # empty_branch=True so the disposition's empty-diff check fires.
    ctx = _arch_ctx(tmp_path, summary, empty_branch=True)
    out = handle_architecture(ctx)
    assert out.decision == "amend"
    assert out.payload.get("empty_diff_forced_amend") is True
    # The synthetic remediation_summary should explain the situation.
    assert "no commits against origin/main" in out.payload.get(
        "remediation_summary", ""
    )


def test_architecture_handler_prose_fallback_catches_all_changes_in_place(
    tmp_path: Path,
) -> None:
    """Observed 2026-05-16 on 472e3ddc: the architect opened with 'All
    changes are in place. Here's a summary of what was changed across
    6 files:' and enumerated each file. The cue list was extended to
    catch this phrasing; the verdict resolves to accept-as-is."""
    summary = (
        "All changes are in place. Here's a summary of what was "
        "changed across 6 files:\n\n"
        "- services/api/treadmill_api/eventbus.py: added otel propagate"
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "accept-as-is"
    assert out.payload.get("parsed_from_prose") is True


def test_architecture_handler_prose_fallback_extracts_amend(
    tmp_path: Path,
) -> None:
    """Prose-fallback for ``amend`` — model says 'the implementation is
    incomplete' or 'needs amendment' without a JSON envelope."""
    summary = (
        "Looking at the diff against the spec, the implementation is "
        "incomplete. The cli.py wiring and custom spans are missing."
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "amend"
    assert out.payload.get("parsed_from_prose") is True


def test_architecture_handler_prose_fallback_yields_to_json_when_present(
    tmp_path: Path,
) -> None:
    """When BOTH a JSON envelope AND prose cues are present, the JSON
    envelope wins (strict path remains primary). The fallback is only
    consulted when no valid JSON block exists."""
    summary = (
        "The implementation is already complete on this branch.\n\n"
        '```json\n{"verdict": "amend", "reasoning": "but on closer '
        'inspection a piece is missing", "target_artifact": "X.py", '
        '"remediation_summary": "wire it up"}\n```'
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    # JSON envelope's ``amend`` wins over the prose's accept-as-is cue.
    assert out.decision == "amend"
    assert out.payload.get("parsed_from_prose") is None


def test_architecture_handler_takes_last_verdict_block(
    tmp_path: Path,
) -> None:
    """Mirroring ADR-0027: when multiple JSON blocks carry verdicts, the
    last one wins (the architect explored alternatives + converged)."""
    summary = (
        "First I considered:\n"
        '```json\n{"verdict": "amend", "reasoning": "first thought", '
        '"target_artifact": "x"}\n```\n'
        "On reflection:\n"
        '```json\n{"verdict": "accept-as-is", "reasoning": "actually fine", '
        '"target_artifact": "x"}\n```'
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "accept-as-is"


def test_architecture_handler_valid_validator_tuning_surfaces_in_payload(
    tmp_path: Path,
) -> None:
    """ADR-0040: when the envelope carries a valid ``validator_tuning``
    sub-object, it is validated via the ValidatorTuning Pydantic model
    and surfaced under ``StepOutput.payload.validator_tuning``."""
    tuning = {
        "rule_slug": "implementation-conforms-to-diagram",
        "check_id": "diagram-match",
        "action": "demote_severity",
        "evidence": "The diff implements the contract; the rule false-positived.",
        "proposed_patch": {"from": "blocking", "to": "warning"},
    }
    envelope = {
        "verdict": "accept-as-is",
        "reasoning": "Reviewer was wrong; work is fine.",
        "target_artifact": "services/api/treadmill_api/coordination/consumer.py",
        "validator_tuning": tuning,
    }
    summary = f"```json\n{json.dumps(envelope)}\n```"
    ctx = _arch_ctx(tmp_path, summary, trigger="self:wf-feedback-deadlock")
    out = handle_architecture(ctx)
    assert out.decision == "accept-as-is"
    assert "validator_tuning" in out.payload
    vt = out.payload["validator_tuning"]
    assert vt["rule_slug"] == "implementation-conforms-to-diagram"
    assert vt["action"] == "demote_severity"
    assert vt["proposed_patch"] == {"from": "blocking", "to": "warning"}


def test_architecture_handler_no_validator_tuning_omits_key(
    tmp_path: Path,
) -> None:
    """When the envelope has no ``validator_tuning`` field, the payload
    must not carry the key at all."""
    summary = (
        '```json\n'
        '{"verdict": "accept-as-is", "reasoning": "gap is acceptable.", '
        '"target_artifact": "workers/agent/AGENT.md"}\n'
        '```'
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "accept-as-is"
    assert "validator_tuning" not in out.payload


def test_architecture_handler_malformed_validator_tuning_drops_with_warn(
    tmp_path: Path,
    caplog: Any,
) -> None:
    """When the envelope carries a ``validator_tuning`` sub-object that
    fails Pydantic validation (missing required fields / wrong literal),
    the step must NOT fail — the malformed tuning is dropped and a WARN
    log is emitted. The routing payload (verdict + dispatch) still lands."""
    import logging

    envelope = {
        "verdict": "accept-as-is",
        "reasoning": "gap accepted",
        "target_artifact": "workers/agent/AGENT.md",
        "validator_tuning": {
            "rule_slug": "some-rule",
            # missing: check_id, action, evidence, proposed_patch
        },
    }
    summary = f"```json\n{json.dumps(envelope)}\n```"
    ctx = _arch_ctx(tmp_path, summary)
    with caplog.at_level(logging.WARNING, logger="treadmill.agent.architecture"):
        out = handle_architecture(ctx)
    assert out.decision == "accept-as-is"
    assert "validator_tuning" not in out.payload
    assert any("validator_tuning" in r.message for r in caplog.records)


# ── ADR-0058: gate-broken verdict ─────────────────────────────────────────────


_GATE_STDERR_EXAMPLE = (
    "--- stderr ---\n"
    "Traceback (most recent call last):\n"
    "  File \"/var/treadmill/workspaces/.../repo/app.py\", line 3, in <module>\n"
    "    import aws_cdk\n"
    "ModuleNotFoundError: No module named 'aws_cdk'\n"
)


def test_architecture_handler_gate_broken_verdict_emits_parked_dispatch(
    tmp_path: Path,
) -> None:
    """ADR-0058: the disposition accepts ``gate-broken`` + a non-empty
    ``gate_log_excerpt``, surfaces both at top-level on the payload, and
    emits a parked dispatch payload (``workflow_id=None`` so no successor
    workflow_run is dispatched + ``intent='gate-broken-park'``). The
    API-side ``maybe_dispatch_gate_broken_escalation`` trigger reads
    the step's top-level ``payload.verdict`` + ``payload.gate_log_excerpt``
    and emits the operator escalation; this disposition just signals
    "no follow-up dispatch from the worker side."""
    summary = (
        '```json\n'
        '{"verdict": "gate-broken", "reasoning": "Trigger B (ralph-loop '
        'deadlock): cdk synth fails because aws_cdk is not in the worker '
        'sandbox. Code is logically complete; gate is unsatisfiable.", '
        '"target_artifact": "tasks/<id>/validation", '
        f'"gate_log_excerpt": {json.dumps(_GATE_STDERR_EXAMPLE)}}}\n'
        '```'
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "gate-broken"
    assert out.payload["dispatch"]["workflow_id"] is None
    assert out.payload["dispatch"]["intent"] == "gate-broken-park"
    assert "ModuleNotFoundError" in out.payload["gate_log_excerpt"]


def test_architecture_handler_gate_broken_without_excerpt_raises(
    tmp_path: Path,
) -> None:
    """Per ADR-0058, ``verdict='gate-broken'`` without a non-empty
    ``gate_log_excerpt`` is a parse failure. The disposition forbids
    the worker from emitting an excerpt-less gate-broken so the step
    fails fast rather than dispatching an empty-evidence escalation."""
    summary = (
        '```json\n'
        '{"verdict": "gate-broken", "reasoning": "The gate is broken", '
        '"target_artifact": "tasks/<id>/validation"}\n'
        '```'
    )
    ctx = _arch_ctx(tmp_path, summary)
    with pytest.raises(ArchitectVerdictParseError) as exc_info:
        handle_architecture(ctx)
    assert "gate_log_excerpt" in str(exc_info.value)


def test_architecture_handler_prose_fallback_extracts_gate_broken(
    tmp_path: Path,
) -> None:
    """Prose-fallback parser recognizes ``trigger b (ralph-loop deadlock)``
    and ``the gate is broken`` style cues. Since prose can't recover
    the original gate stderr, the fallback synthesizes a placeholder
    excerpt that satisfies the gate_log_excerpt requirement; the
    disposition still emits gate-broken with the prose reasoning."""
    summary = (
        "After reviewing the loop, this is Trigger B (ralph-loop "
        "deadlock). The author has produced logically-complete code "
        "but the deterministic gate cannot be satisfied. The gate is "
        "broken — the worker sandbox is missing the tooling the gate "
        "requires."
    )
    ctx = _arch_ctx(tmp_path, summary)
    out = handle_architecture(ctx)
    assert out.decision == "gate-broken"
    assert "prose-parsed" in out.payload["gate_log_excerpt"]
    assert out.payload.get("parsed_from_prose") is True


# ── crystallization handler (wf-crystallize-learning) ─────────────────────────


from treadmill_agent.runner_dispositions.crystallization import (
    CrystallizationVerdictParseError,
    handle as handle_crystallization,
    _find_learning_file,
    _parse_frontmatter,
)


def _crystal_summary(
    verdict: str,
    learning_slug: str,
    proposed_rule_slug: str | None = "my-rule",
    reasoning: str = "test reasoning",
) -> str:
    envelope: dict[str, Any] = {
        "verdict": verdict,
        "reasoning": reasoning,
        "learning_slug": learning_slug,
    }
    if proposed_rule_slug is not None:
        envelope["proposed_rule_slug"] = proposed_rule_slug
    return f"Judge output.\n\n```json\n{json.dumps(envelope)}\n```\n"


def _crystal_ctx(
    tmp_path: Path,
    summary: str,
    *,
    role_id: str = "role-crystallization-judge",
    workflow_id: str = "wf-crystallize-learning",
) -> DispositionContext:
    _bare, clone = _init_bare_and_clone(tmp_path)
    return _disp_ctx(
        repo_dir=clone,
        output_kind="analysis",
        role_id=role_id,
        workflow_id=workflow_id,
        summary=summary,
    )


def test_crystallization_judge_ready_verdict(tmp_path: Path) -> None:
    """``ready`` verdict emits dispatch payload to step 2; no git side effects."""
    summary = _crystal_summary("ready", "my-learning", "my-rule")
    ctx = _crystal_ctx(tmp_path, summary)
    out = handle_crystallization(ctx)
    assert out.decision == "ready"
    assert out.commit_sha is None
    assert out.payload["verdict"] == "ready"
    assert out.payload["learning_slug"] == "my-learning"
    assert out.payload["proposed_rule_slug"] == "my-rule"
    dispatch = out.payload["dispatch"]
    assert dispatch["workflow_id"] == "wf-crystallize-learning"
    assert dispatch["step"] == "crystallize"
    assert dispatch["learning_slug"] == "my-learning"
    assert dispatch["proposed_rule_slug"] == "my-rule"
    assert dispatch["task_id"] == ctx.ctx.task_id


def test_crystallization_judge_not_ready_verdict(tmp_path: Path) -> None:
    """``not-ready`` updates the learning's frontmatter with backoff dates
    and appends the reasoning to the Notes section, then commits + pushes."""
    _bare, clone = _init_bare_and_clone(tmp_path)

    # Write a learning file in the repo.
    learning_dir = clone / "docs" / "learnings"
    learning_dir.mkdir(parents=True)
    learning_file = learning_dir / "2026-05-01-my-learning.md"
    learning_file.write_text(
        "---\ndate: 2026-05-01\nstatus: captured\n---\n\n# My Learning\n\nBody.\n"
    )

    summary = _crystal_summary("not-ready", "my-learning", reasoning="needs more evidence")
    ctx = _disp_ctx(
        repo_dir=clone,
        output_kind="analysis",
        role_id="role-crystallization-judge",
        workflow_id="wf-crystallize-learning",
        summary=summary,
    )
    out = handle_crystallization(ctx)
    assert out.decision == "not-ready"
    assert out.commit_sha is not None  # committed the frontmatter update
    assert out.payload["verdict"] == "not-ready"
    assert out.payload["learning_slug"] == "my-learning"

    # Frontmatter updated with backoff fields.
    updated = learning_file.read_text()
    fm, body = _parse_frontmatter(updated)
    assert "last_crystallization_check" in fm
    assert "next_crystallization_check" in fm
    assert fm["crystallization_check_count"] == "1"

    # Reasoning captured in Notes.
    assert "needs more evidence" in body
    assert "## Notes" in body


def test_crystallization_judge_defer_verdict(tmp_path: Path) -> None:
    """``defer`` is a no-op: no git side effects, decision == 'defer'."""
    summary = _crystal_summary("defer", "my-learning", proposed_rule_slug=None)
    ctx = _crystal_ctx(tmp_path, summary)
    out = handle_crystallization(ctx)
    assert out.decision == "defer"
    assert out.commit_sha is None
    assert out.payload["verdict"] == "defer"
    assert out.payload["learning_slug"] == "my-learning"


def test_crystallization_judge_missing_envelope(tmp_path: Path) -> None:
    """No JSON block with a valid verdict → CrystallizationVerdictParseError."""
    ctx = _crystal_ctx(tmp_path, "I thought about it but didn't decide.")
    with pytest.raises(CrystallizationVerdictParseError, match="JSON block"):
        handle_crystallization(ctx)


@pytest.mark.parametrize(
    "prose, expected_verdict",
    [
        (
            "After careful review, this learning is ready to crystallize "
            "into a rule. learning_slug: my-learning",
            "ready",
        ),
        (
            "The learning is not-ready for promotion. It needs more evidence "
            "before we can promote it. learning_slug: my-learning",
            "not-ready",
        ),
        (
            "This learning should be deferred for now. learning_slug: my-learning",
            "defer",
        ),
    ],
)
def test_crystallization_judge_prose_verdict_fallback(
    tmp_path: Path, prose: str, expected_verdict: str,
) -> None:
    """Prose-only output (no JSON envelope) is parsed via cue matching.

    Verifies that each of the three crystallization verdicts can be extracted
    from prose when the judge omits the fenced JSON block.
    """
    ctx = _crystal_ctx(tmp_path, prose)
    out = handle_crystallization(ctx)
    assert out.decision == expected_verdict
    assert out.payload["verdict"] == expected_verdict
    assert out.payload["learning_slug"] == "my-learning"


def test_crystallization_crystallize_writes_rule_yaml(tmp_path: Path) -> None:
    """Step 2: architect output → rule YAML + check.sh written to repo,
    learning status updated, commit + push."""
    _bare, clone = _init_bare_and_clone(tmp_path)

    # Place a learning file the crystallize step will update.
    learning_dir = clone / "docs" / "learnings"
    learning_dir.mkdir(parents=True)
    learning_file = learning_dir / "2026-05-01-my-learning.md"
    learning_file.write_text(
        "---\ndate: 2026-05-01\nstatus: captured\n---\n\n# My Learning\n"
    )

    rule_yaml_text = "name: my-rule\ndescription: test rule\nstatus: active\n"
    check_sh_text = "#!/usr/bin/env bash\necho ok"
    architect_summary = (
        "Here is the rule:\n\n"
        f"```yaml\n{rule_yaml_text}```\n\n"
        "And the check:\n\n"
        f"```bash\n{check_sh_text}\n```\n"
    )

    # Simulate prior judge step output in prior_steps.
    prior_output: dict[str, Any] = {
        "summary": "judge output",
        "decision": "ready",
        "commit_sha": None,
        "artifacts": [],
        "payload": {
            "verdict": "ready",
            "learning_slug": "my-learning",
            "proposed_rule_slug": "my-rule",
        },
        "metadata": {},
    }

    from treadmill_agent.api_client import PriorStep

    ctx = DispositionContext(
        ctx=_ctx(
            output_kind="analysis",
            role_id="role-architect",
            workflow_id="wf-crystallize-learning",
        ),
        claude_result=CodeAuthorResult(summary=architect_summary),
        repo_dir=clone,
        branch="task/x-add-thing",
        settings=_settings(),
        is_dry_run=False,
    )
    # Patch prior_steps onto the WorkerContext (frozen dataclass).
    ctx = dataclasses.replace(
        ctx,
        ctx=dataclasses.replace(
            ctx.ctx,
            prior_steps=[
                PriorStep(
                    step_index=0,
                    step_name="judge",
                    role_id="role-crystallization-judge",
                    status="completed",
                    output=prior_output,
                )
            ],
        ),
    )

    out = handle_crystallization(ctx)
    assert out.decision == "pushed"
    assert out.commit_sha is not None
    assert out.payload["rule_slug"] == "my-rule"
    assert out.payload["learning_slug"] == "my-learning"

    # Rule YAML written.
    rule_path = clone / "docs" / "knowledge-base" / "rules" / "my-rule.yaml"
    assert rule_path.exists()
    assert "my-rule" in rule_path.read_text()

    # check.sh written and executable.
    check_sh_path = clone / "tools" / "rule-checks" / "my-rule" / "check.sh"
    assert check_sh_path.exists()
    assert check_sh_path.stat().st_mode & 0o111  # executable

    # Learning status updated.
    updated_fm, _ = _parse_frontmatter(learning_file.read_text())
    assert updated_fm["status"] == "crystallized-into-rule-my-rule"
