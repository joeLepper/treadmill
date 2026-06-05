"""ADR-0074 nothing-to-do short-circuit tests.

Tests the three-clause guard that detects when a task is already complete
(zero commits, validator passed, author accepted) and returns a synthetic
accept-as-is verdict without invoking Claude.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from treadmill_agent.api_client import PriorStep, Role, WorkerContext
from treadmill_agent.claude_code import CodeAuthorResult
from treadmill_agent.config import Settings
from treadmill_agent.runner_dispositions import handle_architecture
from treadmill_agent.runner_dispositions._context import DispositionContext


def _ctx(
    *,
    output_kind: str = "analysis",
    role_id: str = "role-architect",
    workflow_id: str = "wf-architecture-resolve",
    trigger: str = "registered",
    prior_steps: list[PriorStep] | None = None,
) -> WorkerContext:
    return WorkerContext(
        step_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        step_index=2,
        step_name="architect",
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
        pr_number=None,
        prior_steps=prior_steps or [],
        task_validations=[],
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
    prior_steps: list[PriorStep] | None = None,
) -> DispositionContext:
    return DispositionContext(
        ctx=_ctx(prior_steps=prior_steps),
        claude_result=CodeAuthorResult(summary="unused"),
        repo_dir=repo_dir,
        branch="task/x-add-thing",
        settings=_settings(),
        is_dry_run=False,
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


def test_nothing_to_do_short_circuit_all_clauses_hold(tmp_path: Path) -> None:
    """When all three clauses hold (zero commits, validator pass, author
    accept-as-is), the handler returns a synthetic accept-as-is envelope
    WITHOUT calling Claude."""
    _bare, clone = _init_bare_and_clone(tmp_path)

    # Build prior_steps: most recent author (accept-as-is) and validator (pass)
    prior_steps = [
        PriorStep(
            step_index=1,
            step_name="validator",
            role_id="role-validator",
            status="completed",
            output={"decision": "pass"},
        ),
        PriorStep(
            step_index=0,
            step_name="author",
            role_id="role-author",
            status="completed",
            output={"verdict": "accept-as-is", "reasoning": "All done"},
        ),
    ]

    ctx = _disp_ctx(repo_dir=clone, prior_steps=prior_steps)

    # Stub Claude so any invocation raises (should not be called)
    stub_client = MagicMock()
    stub_client.side_effect = RuntimeError("Claude was called; short-circuit failed")

    # Patch the Claude subprocess seam so we can verify it's NOT called
    with patch("treadmill_agent.runner_dispositions.architecture.subprocess") as mock_subprocess:
        mock_subprocess.run.side_effect = stub_client
        out = handle_architecture(ctx)

    # Verify the short-circuit verdict was returned
    assert out.decision == "accept-as-is"
    assert out.payload["short_circuit_reason"] == "nothing-to-do"
    assert out.payload["parsed_from_prose"] is False
    assert out.payload["dispatch"]["workflow_id"] == "wf-doc-amend"
    # Claude was not called
    assert not mock_subprocess.run.called


def test_nothing_to_do_clause_1_fails_commits_exist(tmp_path: Path) -> None:
    """When clause 1 fails (commits exist), architect runs normally."""
    _bare, clone = _init_bare_and_clone(tmp_path)
    # Create a commit on the task branch
    (clone / "WORK.md").write_text("work\n")
    subprocess.run(
        ["git", "-C", str(clone), "add", "-A"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-m", "task work"],
        check=True, capture_output=True,
    )

    prior_steps = [
        PriorStep(
            step_index=1,
            step_name="validator",
            role_id="role-validator",
            status="completed",
            output={"decision": "pass"},
        ),
        PriorStep(
            step_index=0,
            step_name="author",
            role_id="role-author",
            status="completed",
            output={"verdict": "accept-as-is", "reasoning": "All done"},
        ),
    ]

    ctx = _disp_ctx(repo_dir=clone, prior_steps=prior_steps)

    # Mock Claude to return a normal verdict
    normal_verdict = (
        '```json\n'
        '{"verdict": "amend", "reasoning": "Work needs fixing", '
        '"target_artifact": "docs/x.md", '
        '"remediation_summary": "Fix it."}\n'
        '```'
    )
    with patch.object(
        ctx.claude_result, "summary", normal_verdict
    ):
        out = handle_architecture(ctx)

    # Architect ran and returned amend (not short-circuited)
    assert out.decision == "amend"
    assert "short_circuit_reason" not in out.payload


def test_nothing_to_do_clause_2_fails_validator_not_pass(tmp_path: Path) -> None:
    """When clause 2 fails (validator verdict not pass), architect runs."""
    _bare, clone = _init_bare_and_clone(tmp_path)

    prior_steps = [
        PriorStep(
            step_index=1,
            step_name="validator",
            role_id="role-validator",
            status="completed",
            output={"decision": "fail"},  # Not "pass"
        ),
        PriorStep(
            step_index=0,
            step_name="author",
            role_id="role-author",
            status="completed",
            output={"verdict": "accept-as-is", "reasoning": "All done"},
        ),
    ]

    ctx = _disp_ctx(repo_dir=clone, prior_steps=prior_steps)

    # Mock Claude to return a normal verdict
    normal_verdict = (
        '```json\n'
        '{"verdict": "amend", "reasoning": "Validation failed", '
        '"target_artifact": "docs/x.md", '
        '"remediation_summary": "Fix it."}\n'
        '```'
    )
    with patch.object(
        ctx.claude_result, "summary", normal_verdict
    ):
        out = handle_architecture(ctx)

    # Architect ran (not short-circuited)
    assert out.decision == "amend"
    assert "short_circuit_reason" not in out.payload


def test_nothing_to_do_clause_3_fails_author_no_signal(tmp_path: Path) -> None:
    """When clause 3 fails (author has no accept-as-is signal), architect runs."""
    _bare, clone = _init_bare_and_clone(tmp_path)

    prior_steps = [
        PriorStep(
            step_index=1,
            step_name="validator",
            role_id="role-validator",
            status="completed",
            output={"decision": "pass"},
        ),
        PriorStep(
            step_index=0,
            step_name="author",
            role_id="role-author",
            status="completed",
            output={
                "verdict": "pushed",  # Not accept-as-is
                "commit_sha": "abc123",
            },
        ),
    ]

    ctx = _disp_ctx(repo_dir=clone, prior_steps=prior_steps)

    # Mock Claude to return a normal verdict
    normal_verdict = (
        '```json\n'
        '{"verdict": "amend", "reasoning": "Work incomplete", '
        '"target_artifact": "docs/x.md", '
        '"remediation_summary": "Fix it."}\n'
        '```'
    )
    with patch.object(
        ctx.claude_result, "summary", normal_verdict
    ):
        out = handle_architecture(ctx)

    # Architect ran (not short-circuited)
    assert out.decision == "amend"
    assert "short_circuit_reason" not in out.payload


def test_nothing_to_do_author_prose_cue_accept_as_is(tmp_path: Path) -> None:
    """Clause 3 can be satisfied by author prose cues instead of explicit
    verdict field."""
    _bare, clone = _init_bare_and_clone(tmp_path)

    prior_steps = [
        PriorStep(
            step_index=1,
            step_name="validator",
            role_id="role-validator",
            status="completed",
            output={"decision": "pass"},
        ),
        PriorStep(
            step_index=0,
            step_name="author",
            role_id="role-author",
            status="completed",
            output={
                "verdict": "accept-as-is",
                "reasoning": "Implementation is already present; no changes needed.",
            },
        ),
    ]

    ctx = _disp_ctx(repo_dir=clone, prior_steps=prior_steps)

    with patch("treadmill_agent.runner_dispositions.architecture.subprocess") as mock_subprocess:
        mock_subprocess.run.side_effect = RuntimeError("Claude was called")
        out = handle_architecture(ctx)

    assert out.decision == "accept-as-is"
    assert out.payload["short_circuit_reason"] == "nothing-to-do"
    assert not mock_subprocess.run.called


def test_nothing_to_do_git_command_fails_falls_back_to_architect(
    tmp_path: Path,
) -> None:
    """When the git rev-list check fails (edge case: no origin/main,
    detached HEAD, etc.), gracefully fall back to architect."""
    _bare, clone = _init_bare_and_clone(tmp_path)

    prior_steps = [
        PriorStep(
            step_index=1,
            step_name="validator",
            role_id="role-validator",
            status="completed",
            output={"decision": "pass"},
        ),
        PriorStep(
            step_index=0,
            step_name="author",
            role_id="role-author",
            status="completed",
            output={"verdict": "accept-as-is"},
        ),
    ]

    ctx = _disp_ctx(repo_dir=clone, prior_steps=prior_steps)

    # Mock subprocess.run to fail on the git rev-list call
    normal_verdict = (
        '```json\n'
        '{"verdict": "accept-as-is", "reasoning": "All done", '
        '"target_artifact": ""}\n'
        '```'
    )

    def mock_subprocess_run(cmd, **kwargs):
        if isinstance(cmd, list) and "rev-list" in cmd:
            raise subprocess.TimeoutExpired(cmd, 10)
        # For other calls (Claude), return success
        result = MagicMock()
        result.returncode = 0
        return result

    with patch.object(
        ctx.claude_result, "summary", normal_verdict
    ), patch(
        "treadmill_agent.runner_dispositions.architecture.subprocess.run",
        side_effect=mock_subprocess_run,
    ):
        out = handle_architecture(ctx)

    # Architect ran despite clause 1 failing gracefully
    assert out.decision == "accept-as-is"
    # Not short-circuited because git command failed
    assert "short_circuit_reason" not in out.payload


def test_nothing_to_do_no_validator_step_falls_back(tmp_path: Path) -> None:
    """When no validator step exists in prior_steps, clause 2 fails and
    architect runs normally."""
    _bare, clone = _init_bare_and_clone(tmp_path)

    prior_steps = [
        PriorStep(
            step_index=0,
            step_name="author",
            role_id="role-author",
            status="completed",
            output={"verdict": "accept-as-is"},
        ),
    ]

    ctx = _disp_ctx(repo_dir=clone, prior_steps=prior_steps)

    normal_verdict = (
        '```json\n'
        '{"verdict": "amend", "reasoning": "No validator run", '
        '"target_artifact": "docs/x.md", '
        '"remediation_summary": "Fix it."}\n'
        '```'
    )
    with patch.object(
        ctx.claude_result, "summary", normal_verdict
    ):
        out = handle_architecture(ctx)

    assert out.decision == "amend"
    assert "short_circuit_reason" not in out.payload


def test_nothing_to_do_no_author_step_falls_back(tmp_path: Path) -> None:
    """When no author step exists in prior_steps, clause 3 fails and
    architect runs normally."""
    _bare, clone = _init_bare_and_clone(tmp_path)

    prior_steps = [
        PriorStep(
            step_index=1,
            step_name="validator",
            role_id="role-validator",
            status="completed",
            output={"decision": "pass"},
        ),
    ]

    ctx = _disp_ctx(repo_dir=clone, prior_steps=prior_steps)

    normal_verdict = (
        '```json\n'
        '{"verdict": "amend", "reasoning": "No author run", '
        '"target_artifact": "docs/x.md", '
        '"remediation_summary": "Fix it."}\n'
        '```'
    )
    with patch.object(
        ctx.claude_result, "summary", normal_verdict
    ):
        out = handle_architecture(ctx)

    assert out.decision == "amend"
    assert "short_circuit_reason" not in out.payload
