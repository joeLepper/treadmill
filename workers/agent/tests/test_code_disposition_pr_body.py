"""Tests for PR body synthesis in code disposition (ADR-0033).

Per ADR-0033, every PR body must have 5 sections:
  - ## Summary
  - ## Why
  - ## Test plan
  - ## Validation
  - ## Refs
"""

from __future__ import annotations

import uuid
from pathlib import Path

from treadmill_agent.api_client import Role, TaskValidationInfo, WorkerContext
from treadmill_agent.claude_code import CodeAuthorResult
from treadmill_agent.config import Settings
from treadmill_agent.runner_dispositions._context import DispositionContext
from treadmill_agent.runner_dispositions.code import _synthesize_pr_body


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ctx(
    *,
    title: str = "Add a feature",
    description: str | None = "This feature enables X",
    plan_doc_path: str | None = "docs/plans/2026-05-15-my-plan.md",
    task_id: str | None = None,
    task_validations: list[TaskValidationInfo] | None = None,
) -> WorkerContext:
    if task_id is None:
        task_id = str(uuid.uuid4())
    return WorkerContext(
        step_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        step_index=0,
        step_name="step",
        status="pending",
        task_id=task_id,
        plan_id=str(uuid.uuid4()),
        repo="t/r",
        title=title,
        description=description,
        plan_intent="goal",
        plan_doc_path=plan_doc_path,
        workflow_id="wf-author",
        workflow_version=1,
        trigger="registered",
        role=Role(
            id="role-test", model="m", system_prompt="p",
            output_kind="code", skills=[], hooks=[],
        ),
        pr_number=None,
        prior_steps=[],
        task_validations=task_validations or [],
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
    summary: str = "Did the thing\nAlso did another thing",
    title: str = "Add a feature",
    description: str | None = "This feature enables X",
    plan_doc_path: str | None = "docs/plans/2026-05-15-my-plan.md",
    task_id: str | None = None,
    branch: str = "task/abc123-add-feature",
    task_validations: list[TaskValidationInfo] | None = None,
) -> DispositionContext:
    return DispositionContext(
        ctx=_ctx(
            title=title,
            description=description,
            plan_doc_path=plan_doc_path,
            task_id=task_id,
            task_validations=task_validations,
        ),
        claude_result=CodeAuthorResult(summary=summary),
        repo_dir=Path("/tmp/repo"),
        branch=branch,
        settings=_settings(),
        is_dry_run=False,
    )


# ── Tests ────────────────────────────────────────────────────────────────────


def test_synthesize_pr_body_contains_all_five_sections() -> None:
    """Every PR body must contain all 5 sections per ADR-0033."""
    ctx = _disp_ctx()
    body = _synthesize_pr_body(ctx)
    assert "## Summary" in body
    assert "## Why" in body
    assert "## Test plan" in body
    assert "## Validation" in body
    assert "## Refs" in body


def test_synthesize_pr_body_summary_includes_title_and_first_line() -> None:
    """Summary section contains title and first line of model summary."""
    ctx = _disp_ctx(
        title="Fix authentication bug",
        summary="Fixed the login flow\nAlso improved error messages",
    )
    body = _synthesize_pr_body(ctx)
    assert "Fix authentication bug" in body
    assert "Fixed the login flow" in body


def test_synthesize_pr_body_summary_handles_multiline_with_blank_lines() -> None:
    """Summary extracts first line even with blank lines in summary."""
    ctx = _disp_ctx(
        summary="First line of summary\n\nSecond paragraph",
    )
    body = _synthesize_pr_body(ctx)
    assert "First line of summary" in body
    assert "Second paragraph" not in body


def test_synthesize_pr_body_summary_handles_empty_summary() -> None:
    """Summary section uses just title when model summary is empty."""
    ctx = _disp_ctx(summary="")
    body = _synthesize_pr_body(ctx)
    assert "## Summary" in body
    assert "- Add a feature" in body


def test_synthesize_pr_body_why_includes_plan_path_and_description() -> None:
    """Why section contains plan doc path and task description."""
    ctx = _disp_ctx(
        plan_doc_path="docs/plans/2026-05-15-auth-refactor.md",
        description="Refactor authentication to support OAuth2",
    )
    body = _synthesize_pr_body(ctx)
    assert "## Why" in body
    assert "docs/plans/2026-05-15-auth-refactor.md" in body
    assert "Refactor authentication to support OAuth2" in body


def test_synthesize_pr_body_why_handles_missing_plan_path() -> None:
    """Why section omits plan reference when plan_doc_path is None."""
    ctx = _disp_ctx(plan_doc_path=None, description="Fix a bug")
    body = _synthesize_pr_body(ctx)
    assert "## Why" in body
    assert "Fix a bug" in body
    # Should not have Plan: line if no path
    assert "Plan:" not in body or "Plan: `None`" not in body


def test_synthesize_pr_body_why_handles_missing_description() -> None:
    """Why section omits description when it's None."""
    ctx = _disp_ctx(description=None)
    body = _synthesize_pr_body(ctx)
    assert "## Why" in body
    # Should still have the plan path
    assert "docs/plans/2026-05-15-my-plan.md" in body


def test_synthesize_pr_body_test_plan_includes_deterministic_checks() -> None:
    """Test plan section includes deterministic validation checks."""
    ctx = _disp_ctx(
        task_validations=[
            TaskValidationInfo(
                id="test-1",
                kind="deterministic",
                description="Run unit tests",
                script="pytest",
                prompt=None,
            ),
            TaskValidationInfo(
                id="test-2",
                kind="deterministic",
                description="Check linting",
                script="ruff check",
                prompt=None,
            ),
        ]
    )
    body = _synthesize_pr_body(ctx)
    assert "## Test plan" in body
    assert "- [ ] Run unit tests" in body
    assert "- [ ] Check linting" in body


def test_synthesize_pr_body_test_plan_excludes_non_deterministic() -> None:
    """Test plan section excludes non-deterministic checks."""
    ctx = _disp_ctx(
        task_validations=[
            TaskValidationInfo(
                id="llm-check",
                kind="llm-judge",
                description="Verify code quality",
                script=None,
                prompt="Rate this code",
            ),
            TaskValidationInfo(
                id="deterministic-check",
                kind="deterministic",
                description="Run tests",
                script="pytest",
                prompt=None,
            ),
        ]
    )
    body = _synthesize_pr_body(ctx)
    assert "Run tests" in body
    assert "Verify code quality" not in body


def test_synthesize_pr_body_test_plan_default_when_no_checks() -> None:
    """Test plan has default item when no checks are defined."""
    ctx = _disp_ctx(task_validations=[])
    body = _synthesize_pr_body(ctx)
    assert "## Test plan" in body
    assert "- [ ] Verify changes locally" in body


def test_synthesize_pr_body_validation_includes_script_text() -> None:
    """Validation section includes each script in a fenced code block."""
    ctx = _disp_ctx(
        task_validations=[
            TaskValidationInfo(
                id="lint-check",
                kind="deterministic",
                description="Check linting",
                script="ruff check src/",
                prompt=None,
            ),
            TaskValidationInfo(
                id="test-check",
                kind="deterministic",
                description="Run tests",
                script="pytest tests/",
                prompt=None,
            ),
        ]
    )
    body = _synthesize_pr_body(ctx)
    assert "## Validation" in body
    assert "### lint-check" in body
    assert "ruff check src/" in body
    assert "### test-check" in body
    assert "pytest tests/" in body
    assert "```bash" in body


def test_synthesize_pr_body_validation_excludes_non_script_checks() -> None:
    """Validation section excludes checks without scripts."""
    ctx = _disp_ctx(
        task_validations=[
            TaskValidationInfo(
                id="llm-check",
                kind="llm-judge",
                description="Verify quality",
                script=None,
                prompt="Rate this",
            ),
            TaskValidationInfo(
                id="script-check",
                kind="deterministic",
                description="Run script",
                script="./check.sh",
                prompt=None,
            ),
        ]
    )
    body = _synthesize_pr_body(ctx)
    assert "### script-check" in body
    assert "./check.sh" in body
    assert "llm-check" not in body


def test_synthesize_pr_body_validation_default_when_no_scripts() -> None:
    """Validation section has default message when no scripts."""
    ctx = _disp_ctx(task_validations=[])
    body = _synthesize_pr_body(ctx)
    assert "## Validation" in body
    assert "No validation scripts defined" in body


def test_synthesize_pr_body_refs_includes_task_id_plan_branch() -> None:
    """Refs section includes task ID, plan path, and branch."""
    task_id = "task-12345"
    plan_path = "docs/plans/2026-05-15-my-plan.md"
    branch = "task/abc123-my-feature"
    ctx = _disp_ctx(
        task_id=task_id,
        plan_doc_path=plan_path,
        branch=branch,
    )
    body = _synthesize_pr_body(ctx)
    assert "## Refs" in body
    assert f"- Task: {task_id}" in body
    assert f"- Plan: {plan_path}" in body
    assert f"- Branch: {branch}" in body


def test_synthesize_pr_body_refs_omits_plan_when_missing() -> None:
    """Refs section omits plan entry when plan_doc_path is None."""
    task_id = "task-12345"
    ctx = _disp_ctx(task_id=task_id, plan_doc_path=None)
    body = _synthesize_pr_body(ctx)
    assert "## Refs" in body
    assert f"- Task: {task_id}" in body
    # Verify Plan line isn't there
    lines = body.split('\n')
    plan_lines = [l for l in lines if l.startswith("- Plan:")]
    assert len(plan_lines) == 0


def test_synthesize_pr_body_refs_always_has_task_and_branch() -> None:
    """Refs section always includes task ID and branch."""
    task_id = "task-xyz"
    branch = "task/xyz-feature"
    ctx = _disp_ctx(task_id=task_id, branch=branch, plan_doc_path=None)
    body = _synthesize_pr_body(ctx)
    assert f"- Task: {task_id}" in body
    assert f"- Branch: {branch}" in body


def test_synthesize_pr_body_complete_example() -> None:
    """Full integration test with all sections populated."""
    ctx = _disp_ctx(
        title="Add OAuth2 support",
        summary="Implemented OAuth2 authentication flow\nAlso added refresh tokens",
        description="Refactor auth system to support OAuth2 provider integration",
        plan_doc_path="docs/plans/2026-05-15-oauth2.md",
        task_id="task-oauth2-001",
        branch="task/abc-oauth2-support",
        task_validations=[
            TaskValidationInfo(
                id="unit-tests",
                kind="deterministic",
                description="Run unit tests",
                script="pytest tests/auth/",
                prompt=None,
            ),
            TaskValidationInfo(
                id="lint",
                kind="deterministic",
                description="Check code style",
                script="ruff check src/",
                prompt=None,
            ),
        ]
    )
    body = _synthesize_pr_body(ctx)

    # Verify all sections are present and populated
    assert "## Summary" in body
    assert "Add OAuth2 support" in body
    assert "Implemented OAuth2 authentication flow" in body

    assert "## Why" in body
    assert "docs/plans/2026-05-15-oauth2.md" in body
    assert "Refactor auth system to support OAuth2 provider integration" in body

    assert "## Test plan" in body
    assert "- [ ] Run unit tests" in body
    assert "- [ ] Check code style" in body

    assert "## Validation" in body
    assert "### unit-tests" in body
    assert "pytest tests/auth/" in body
    assert "### lint" in body
    assert "ruff check src/" in body

    assert "## Refs" in body
    assert "- Task: task-oauth2-001" in body
    assert "- Plan: docs/plans/2026-05-15-oauth2.md" in body
    assert "- Branch: task/abc-oauth2-support" in body
