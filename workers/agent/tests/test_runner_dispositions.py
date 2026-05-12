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
)
from treadmill_agent.runner_dispositions._context import DispositionContext
from treadmill_agent.runner_dispositions.plan_doc import PlanDocScopeError
from treadmill_agent.runner_dispositions.review import (
    MissingContextError,
    _parse_verdict_marker,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ctx(
    *,
    output_kind: str = "code",
    pr_number: int | None = None,
    role_id: str = "role-test",
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
        workflow_id="wf-test",
        workflow_version=1,
        trigger="registered",
        role=Role(
            id=role_id, model="m", system_prompt="p",
            output_kind=output_kind, skills=[], hooks=[],
        ),
        pr_number=pr_number,
        prior_steps=[],
    )


def _settings() -> Settings:
    return Settings(
        api_url="http://fake",
        work_queue_url="http://sqs/q",
        events_topic_arn="arn",
        aws_endpoint_url=None,
        aws_region="us-east-1",
        repo_mode="local",
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
    is_dry_run: bool = False,
) -> DispositionContext:
    return DispositionContext(
        ctx=_ctx(output_kind=output_kind, pr_number=pr_number, role_id=role_id),
        claude_result=CodeAuthorResult(summary=summary),
        repo_dir=repo_dir,
        branch="task/x-add-thing",
        settings=_settings(),
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
    is today's runner behavior preserved into the code handler."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    # No file changes — diff is empty.
    ctx = _disp_ctx(repo_dir=clone)
    with pytest.raises(claude_code.CodeAuthorError, match="no changes"):
        handle_code(ctx)


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


def test_review_handler_raises_without_pr_number(tmp_path: Path) -> None:
    """A review-kind step against a task that hasn't opened a PR yet
    is a config error — raise loudly so the operator sees it as a
    clean step.failed."""
    ctx = _disp_ctx(
        repo_dir=tmp_path, output_kind="review", pr_number=None,
    )
    with pytest.raises(MissingContextError, match="pr_number"):
        handle_review(ctx)


def test_review_handler_invokes_gh_pr_review_with_parsed_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review handler shells out to ``gh.pr_review`` with the
    verdict parsed from Claude's output and the PR number from the
    step context. The decision maps to ADR-0012's wf-review value set."""
    calls: list[dict[str, Any]] = []

    def _fake(pr_number, *, verdict, body, cwd=None):
        calls.append({"pr": pr_number, "verdict": verdict, "body": body})

    monkeypatch.setattr(gh, "pr_review", _fake)

    ctx = _disp_ctx(
        repo_dir=tmp_path, output_kind="review", pr_number=42,
        summary="Reviewed the diff carefully.\n\nVERDICT: approve\n",
    )
    out = handle_review(ctx)
    assert calls == [{"pr": 42, "verdict": "approve", "body": ctx.claude_result.summary}]
    # ADR-0012 mapping: approve → approved.
    assert out.decision == "approved"
    review_artifacts = [a for a in out.artifacts if a.kind == "pr_review"]
    assert len(review_artifacts) == 1
    assert review_artifacts[0].value == "approve"
    assert out.payload["pr_number"] == 42
    assert out.payload["verdict"] == "approve"


def test_review_handler_skips_gh_in_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run path doesn't touch the gh CLI — the envelope still
    reflects the parsed verdict so tests can exercise the marker
    convention without a live GitHub."""
    def _fail(*_args, **_kwargs):
        raise AssertionError("gh.pr_review should not be called in dry-run")

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


# ── runner-level dispatch ────────────────────────────────────────────────────


def test_runner_dispatch_table_covers_all_four_v0_kinds() -> None:
    """The dispatch table has exactly the four ADR-0022 v0 kinds.
    A future kind (e.g. when the Ralph-loop validation ADR lands) is
    an intentional addition; this test is the tripwire."""
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
