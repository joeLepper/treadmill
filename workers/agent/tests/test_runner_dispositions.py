"""Per-kind disposition handler tests (ADR-0022).

One test per handler — exercises the four kinds (``code``,
``review``, ``analysis``, ``plan_doc``) against their
``DispositionContext``, mostly via direct invocation with synthetic
contexts so the tests stay fast + deterministic.

The runner-level dispatch (the table that picks the handler) is
exercised in ``test_runner.py``.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from treadmill_agent import claude_code, gh
from treadmill_agent.api_client import Role, WorkerContext
from treadmill_agent.claude_code import CodeAuthorResult
from treadmill_agent.config import Settings
from treadmill_agent.runner_dispositions import (
    handle_analysis,
    handle_code,
    handle_plan_doc,
    handle_review,
    handle_validation,
)
from treadmill_agent.runner_dispositions._context import DispositionContext
from treadmill_agent.runner_dispositions.plan_doc import PlanDocScopeError
from treadmill_agent.runner_dispositions.review import (
    MissingContextError,
    ReviewVerdict,
    _extract_json_block,
    _parse_review_envelope,
    _parse_verdict_marker,
    _strip_json_block,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ctx(
    *,
    output_kind: str = "code",
    pr_number: int | None = None,
    role_id: str = "role-test",
    workflow_id: str = "wf-test",
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
        trigger="registered",
        role=Role(
            id=role_id, model="m", system_prompt="p",
            output_kind=output_kind, skills=[], hooks=[],
        ),
        pr_number=pr_number,
        prior_steps=[],
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
) -> DispositionContext:
    return DispositionContext(
        ctx=_ctx(
            output_kind=output_kind,
            pr_number=pr_number,
            role_id=role_id,
            workflow_id=workflow_id,
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


# ── review handler ──────────────────────────────────────────────────────────


def test_parse_verdict_marker_picks_approve() -> None:
    assert _parse_verdict_marker("blah\nVERDICT: approve\n") == "approve"


def test_parse_verdict_marker_picks_request_changes() -> None:
    assert (
        _parse_verdict_marker("blah\nVERDICT: request_changes\n")
        == "request_changes"
    )


def test_parse_verdict_marker_picks_comment() -> None:
    assert _parse_verdict_marker("blah\nVERDICT: comment\n") == "comment"


def test_parse_verdict_marker_defaults_to_comment_when_absent() -> None:
    """The safe default — no marker means ``comment``, never accidentally
    approves a PR Treadmill can't actually evaluate."""
    assert _parse_verdict_marker("just text, no marker") == "comment"


def test_parse_verdict_marker_takes_last_match_when_ambiguous() -> None:
    """Q22.c — multiple markers means the prompt is wrong; the handler
    takes the LAST line so a corrected verdict at the end wins."""
    text = "VERDICT: approve\n...changed my mind...\nVERDICT: request_changes"
    assert _parse_verdict_marker(text) == "request_changes"


# ── Marker-decoration tolerance (tourniquet for the PR #10 deathloop) ──────────
#
# The strict regex used to reject everything except ``^VERDICT: <verb>$``;
# the model occasionally adds Markdown emphasis under the "end your
# response with one of these lines" instruction, and that defeated the
# parse → verdict defaulted to ``comment`` → mergeability collapsed to
# ``blocked-on-review`` → the runner re-authored → deathloop. Each case
# below is a real or near-real decoration we have observed (or trivially
# expect to observe given the prompt's emphasis instructions).


def test_parse_verdict_marker_tolerates_bold_wrap() -> None:
    """``**VERDICT: request_changes**`` — the live PR #10 failure case."""
    assert (
        _parse_verdict_marker("blah\n**VERDICT: request_changes**\n")
        == "request_changes"
    )


def test_parse_verdict_marker_tolerates_italic_wrap() -> None:
    assert (
        _parse_verdict_marker("blah\n*VERDICT: approve*\n") == "approve"
    )


def test_parse_verdict_marker_tolerates_backtick_wrap() -> None:
    assert _parse_verdict_marker("blah\n`VERDICT: approve`\n") == "approve"


def test_parse_verdict_marker_tolerates_double_wrap() -> None:
    """The model has been seen to double-wrap (``**`VERDICT: ...`**``)."""
    assert (
        _parse_verdict_marker("blah\n**`VERDICT: request_changes`**\n")
        == "request_changes"
    )


def test_parse_verdict_marker_tolerates_leading_bullet() -> None:
    assert (
        _parse_verdict_marker("Summary follows.\n- VERDICT: approve\n")
        == "approve"
    )


def test_parse_verdict_marker_tolerates_leading_blockquote() -> None:
    assert (
        _parse_verdict_marker("> VERDICT: request_changes\n")
        == "request_changes"
    )


def test_parse_verdict_marker_tolerates_trailing_punctuation() -> None:
    assert (
        _parse_verdict_marker("...\nVERDICT: approve.\n") == "approve"
    )


def test_parse_verdict_marker_rejects_unknown_verb() -> None:
    """Tolerance widens decorations, NOT the verdict vocabulary —
    ``VERDICT: lgtm`` is still nonsense and must fall through to the
    safe default rather than silently rewriting to e.g. ``approve``."""
    assert _parse_verdict_marker("VERDICT: lgtm") == "comment"


def test_parse_verdict_marker_last_wins_across_decorated_lines() -> None:
    """Last-marker-wins still holds when each candidate is decorated
    differently — the normalization must not collapse multiple matches
    into the first."""
    text = (
        "**VERDICT: approve**\n"
        "Actually on reflection:\n"
        "- VERDICT: request_changes\n"
    )
    assert _parse_verdict_marker(text) == "request_changes"


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


def test_parse_review_envelope_falls_through_to_regex_on_invalid_json() -> None:
    """When the JSON block is malformed (syntactic), the parser falls
    through to the regex tourniquet. Rationale is None because the
    regex path can't recover one."""
    text = (
        "Reviewed the diff.\n\n"
        "```json\n"
        '{"verdict": "approve", but this is not valid json\n'
        "```\n\n"
        "VERDICT: approve\n"
    )
    verdict, rationale = _parse_review_envelope(text)
    assert verdict == "approve"
    assert rationale is None


def test_parse_review_envelope_falls_through_on_invalid_verdict_value() -> None:
    """When the JSON block parses but the verdict is outside the
    closed value-set, fall through to regex (or default)."""
    text = (
        "```json\n"
        '{"verdict": "lgtm", "rationale": "looks good"}\n'
        "```\n"
        "VERDICT: approve\n"
    )
    verdict, rationale = _parse_review_envelope(text)
    assert verdict == "approve"
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
    """No JSON, no VERDICT line → safe default ``comment``."""
    verdict, rationale = _parse_review_envelope("Just prose, no marker at all.")
    assert verdict == "comment"
    assert rationale is None


def test_parse_review_envelope_regex_explicit_comment() -> None:
    """The regex tourniquet can return ``comment`` explicitly. The
    envelope parser distinguishes 'model explicitly said comment'
    from 'we fell to the safe default' but both yield the same
    verdict — the rationale is None either way (regex path
    can't recover one)."""
    text = "Some notes.\nVERDICT: comment\n"
    verdict, rationale = _parse_review_envelope(text)
    assert verdict == "comment"
    assert rationale is None


def test_review_handler_strips_json_fence_from_posted_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q27.c: the JSON fence is stripped from the body sent to gh
    pr comment so the PR-page reader sees clean prose. The verdict
    + rationale flow through the StepOutput envelope, not through
    the comment body."""
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
    # The JSON key is gone (the header line contains the bare word
    # "verdict" by design, but the fenced ``"verdict":`` form does
    # not survive the strip).
    assert '"verdict"' not in captured[0]
    assert '"rationale"' not in captured[0]
    # The surrounding prose survives.
    assert "merge-key logic" in captured[0]
    # The verdict + rationale travel via the StepOutput envelope.
    assert out.decision == "changes_requested"
    assert out.payload["verdict"] == "request_changes"
    assert out.payload["rationale"] == "fix the merge key"


def test_review_handler_dry_run_still_parses_per_q27d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        summary="Reviewed the diff carefully.\n\nVERDICT: approve\n",
    )
    out = handle_review(ctx)
    assert len(calls) == 1
    assert calls[0]["pr"] == 42
    assert calls[0]["body"].startswith("## Treadmill review verdict: approve\n\n")
    assert ctx.claude_result.summary in calls[0]["body"]
    # ADR-0012 mapping: approve → approved.
    assert out.decision == "approved"
    review_artifacts = [a for a in out.artifacts if a.kind == "pr_review"]
    assert len(review_artifacts) == 1
    assert review_artifacts[0].value == "approve"
    assert out.payload["pr_number"] == 42
    assert out.payload["verdict"] == "approve"


def test_review_handler_comment_header_uses_human_verb_for_request_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        summary="Diff has correctness issues.\nVERDICT: request_changes\n",
    )
    handle_review(ctx)
    assert captured[0].startswith("## Treadmill review verdict: request changes\n\n")


def test_review_handler_skips_gh_in_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        summary="ok\nVERDICT: request_changes\n",
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
    def _fake_deterministic(check, repo_dir, timeout_seconds):
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

    def _fake_deterministic(check, repo_dir, timeout_seconds):
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

    def _fake_deterministic(check, repo_dir, timeout_seconds):
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


# ── runner-level dispatch ────────────────────────────────────────────────────


def test_runner_dispatch_table_covers_all_four_v0_kinds() -> None:
    """The dispatch table has exactly the four ADR-0022 v0 kinds.
    Per ADR-0029, validation dispatches by workflow_id (not in the
    output_kind table), so it's not counted here.
    This test is the tripwire for new kinds."""
    from treadmill_agent.runner import DISPOSITIONS

    assert set(DISPOSITIONS) == {"code", "review", "analysis", "plan_doc"}


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
