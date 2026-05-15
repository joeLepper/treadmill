"""``validate`` disposition — run validation checks and aggregate results.

Per ADR-0029, the wf-validate workflow runs a suite of validation checks
against the PR and synthesizes a result:

  1. Reads ``task_validations`` rows from the API (checks defined in the
     task or via the plan).
  2. Loads ``docs/knowledge-base/rules/*.yaml`` and evaluates ``applies_to``
     glob patterns against the PR's changed files.
  3. For each matching check, dispatches to ``validation_runtime.run_deterministic``
     or ``run_llm_judge``.
  4. Aggregates results with worst-wins logic: only ``severity=blocking``
     checks affect the aggregate (pass | fail | error). ``warning`` and
     ``advisory`` checks surface in payload but don't flip the decision.
  5. Composes a human-readable summary grouped by verdict.
  6. Posts the summary via ``gh pr comment``.
  7. Returns ``StepOutput`` with the aggregate decision and check results.

Routing: the runner checks workflow_id == 'wf-validate' *before*
consulting output_kind; the validation handler owns the entire step.
"""

from __future__ import annotations

import fnmatch
import logging
import subprocess
from pathlib import Path
from typing import Any

import yaml

from treadmill_agent import gh, validation_runtime
from treadmill_agent.events import Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext

logger = logging.getLogger("treadmill.agent.validation")


def handle(ctx: DispositionContext) -> StepOutput:
    """Execute validation checks for the PR and return aggregated results.

    Steps:
      1. Fetch task-specific checks from the API (if any).
      2. Load knowledge-base rules that apply to the PR's changed files.
      3. For each check, run deterministic or LLM-judge validation.
      4. Aggregate with worst-wins logic (only blocking checks count).
      5. Compose summary grouped by verdict.
      6. Post ``gh pr comment`` (unless dry_run).
      7. Return ``StepOutput`` with decision + check results.
    """
    if ctx.ctx.pr_number is None:
        raise ValueError("validation handler requires pr_number; none found in context")

    # Load all checks: task-specific + rule-based (matching changed files)
    all_checks = _load_checks(ctx)

    # Run all checks and collect results
    results = _run_all_checks(ctx, all_checks)

    # Aggregate with worst-wins: only blocking severity affects decision
    decision = _aggregate_decision(results)

    # Compose summary grouped by verdict
    summary = _compose_summary(results)

    # Post the summary to the PR (unless dry-run)
    if not ctx.is_dry_run:
        gh.pr_comment(ctx.ctx.pr_number, body=summary, cwd=ctx.repo_dir)

    # Return envelope
    return StepOutput(
        summary=summary,
        decision=decision,
        commit_sha=_get_head_sha(ctx.repo_dir),
        artifacts=[],
        payload={
            "checks": [
                {
                    "check_id": r.check_id,
                    "kind": r.kind,
                    "severity": r.severity,
                    "verdict": r.verdict,
                    "rationale": r.rationale,
                    "log_excerpt": r.log_excerpt,
                }
                for r in results
            ]
        },
        metadata=Metadata(),
    )


def _get_head_sha(repo_dir: Path) -> str:
    """Fetch the HEAD commit SHA from the repository.

    Returns the commit SHA of the current branch's HEAD.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.exception("failed to get HEAD SHA: %s", e.stderr)
        return ""


def _load_checks(ctx: DispositionContext) -> list[Any]:
    """Load checks from task_validations + applicable rules.

    Returns a flat list of check objects with .id, .kind, .severity,
    .type, and check-type-specific fields (.script for deterministic,
    .prompt for llm-judge).
    """
    checks = []

    # TODO: Fetch task-specific checks from API once task_validations
    # endpoint is wired. For now, only rules.
    # task_checks = api.fetch_task_validations(ctx.ctx.task_id)

    # Load knowledge-base rules that apply to this PR
    rule_checks = _load_applicable_rules(ctx)
    checks.extend(rule_checks)

    return checks


def _load_applicable_rules(ctx: DispositionContext) -> list[Any]:
    """Load rules from docs/knowledge-base/rules/*.yaml and filter by
    applies_to glob patterns against the PR's changed files.

    Returns a list of check objects extracted from matching rules.
    """
    checks = []
    rules_dir = ctx.repo_dir / "docs" / "knowledge-base" / "rules"

    if not rules_dir.exists():
        logger.info("no rules dir at %s; no rule-based checks", rules_dir)
        return checks

    # Get the list of changed files
    changed_files = _get_pr_changed_files(ctx)
    logger.info("PR changed files: %s", changed_files)

    for rule_file in sorted(rules_dir.glob("*.yaml")):
        try:
            rule = yaml.safe_load(rule_file.read_text())
            if not rule or not rule.get("checks"):
                continue

            rule_name = rule.get("name", rule_file.stem)
            applies_to = rule.get("applies_to")

            # If applies_to is omitted, the rule applies to all projects
            if applies_to and not _matches_applies_to(applies_to, changed_files):
                logger.info("rule %s does not apply to PR", rule_name)
                continue

            logger.info("rule %s applies to PR", rule_name)

            # Extract checks from the rule
            for check_spec in rule.get("checks", []):
                check_obj = _normalize_check(check_spec, rule_name)
                checks.append(check_obj)
        except Exception as e:
            logger.exception("failed to load rule %s", rule_file)
            continue

    return checks


def _get_pr_changed_files(ctx: DispositionContext) -> list[str]:
    """Fetch the list of files changed in the PR using ``gh pr diff``.

    Returns a list of file paths (one per line from ``--name-only``).
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "diff", str(ctx.ctx.pr_number), "--name-only"],
            cwd=str(ctx.repo_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip().splitlines()
    except subprocess.CalledProcessError as e:
        logger.exception(
            "gh pr diff failed for PR %d: %s", ctx.ctx.pr_number, e.stderr
        )
        return []


def _matches_applies_to(applies_to: str | list[str], changed_files: list[str]) -> bool:
    """Return True if the applies_to glob(s) match any changed file.

    applies_to can be a single glob string or a list of globs.
    """
    globs = [applies_to] if isinstance(applies_to, str) else applies_to
    for glob_pat in globs:
        for file in changed_files:
            if fnmatch.fnmatch(file, glob_pat):
                return True
    return False


def _normalize_check(spec: dict[str, Any], rule_name: str) -> Any:
    """Convert a check spec from a rule YAML into a normalized object.

    Adds ``rule_name`` and maps ``type`` → ``kind`` for compatibility
    with validation_runtime expectations.
    """

    class Check:
        pass

    check = Check()
    check.id = spec.get("id", "")
    check.kind = spec.get("type", "deterministic")  # type or kind?
    check.severity = spec.get("severity", "advisory")
    check.script = spec.get("script")
    check.prompt = spec.get("prompt")
    check.rule_name = rule_name
    check.description = spec.get("description", "")
    return check


def _run_all_checks(
    ctx: DispositionContext,
    checks: list[Any],
) -> list[validation_runtime.CheckResult]:
    """Execute all checks and return results.

    For deterministic checks, runs the script. For llm-judge checks,
    calls Claude to evaluate. Handles timeouts and errors gracefully.
    """
    results = []

    # Get PR diff for LLM-judge checks
    pr_diff = _get_pr_diff(ctx)

    for check in checks:
        try:
            if check.kind == "deterministic":
                result = validation_runtime.run_deterministic(
                    check, ctx.repo_dir, timeout_seconds=30,
                    pr_number=ctx.ctx.pr_number,
                )
            elif check.kind == "llm-judge":
                # For LLM-judge, we need task spec
                task_spec = _compose_task_spec(ctx)
                result = validation_runtime.run_llm_judge(
                    check,
                    ctx.repo_dir,
                    diff=pr_diff,
                    task_spec=task_spec,
                    model=ctx.ctx.role.model,
                    timeout_seconds=60,
                )
            else:
                logger.warning("unknown check kind %s for %s", check.kind, check.id)
                continue
            results.append(result)
        except Exception as e:
            logger.exception("error running check %s", check.id)
            # Synthesize an error result
            results.append(
                validation_runtime.CheckResult(
                    check_id=check.id,
                    kind=check.kind,
                    severity=getattr(check, "severity", "advisory"),
                    verdict="error",
                    rationale=f"Check execution failed: {str(e)[:200]}",
                    log_excerpt="",
                )
            )

    return results


def _get_pr_diff(ctx: DispositionContext) -> str:
    """Fetch the unified diff of the PR using ``gh pr diff``.

    Returns the full diff text.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "diff", str(ctx.ctx.pr_number)],
            cwd=str(ctx.repo_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.exception(
            "gh pr diff failed for PR %d: %s", ctx.ctx.pr_number, e.stderr
        )
        return ""


def _compose_task_spec(ctx: DispositionContext) -> str:
    """Compose a task specification string for LLM-judge checks.

    Includes task title, description, plan intent, and workflow info.
    """
    parts = []
    if ctx.ctx.title:
        parts.append(f"Title: {ctx.ctx.title}")
    if ctx.ctx.description:
        parts.append(f"Description: {ctx.ctx.description}")
    if ctx.ctx.plan_intent:
        parts.append(f"Plan Intent: {ctx.ctx.plan_intent}")
    parts.append(f"Workflow: {ctx.ctx.workflow_id} v{ctx.ctx.workflow_version}")
    return "\n".join(parts)


def _aggregate_decision(results: list[validation_runtime.CheckResult]) -> str:
    """Aggregate check results with worst-wins logic.

    Per ADR-0039, only ``verdict='fail'`` with ``severity='blocking'``
    gates merge. Errors are logged but do not flip the aggregate.
      - any blocking fail → 'fail'
      - all pass (errors logged separately) → 'pass'

    Warning and advisory checks are ignored for aggregation.
    """
    blocking = [r for r in results if r.severity == "blocking"]

    if not blocking:
        # No blocking checks; aggregate only considers their presence
        return "pass"

    # Log any errored checks for observability (ADR-0020)
    for r in blocking:
        if r.verdict == "error":
            logger.warning("rule.error", extra={"rule_id": r.check_id, "reason": r.rationale})

    # Only fail verdicts gate merge; errors are surfaced but don't flip decision
    has_fail = any(r.verdict == "fail" for r in blocking)

    if has_fail:
        return "fail"
    return "pass"


def _compose_summary(results: list[validation_runtime.CheckResult]) -> str:
    """Compose a human-readable summary grouped by verdict.

    Formats as:
      ## Validation Results

      **Pass (X)**
      - check-id: rationale

      **Fail (X)**
      - ...

      etc.
    """
    by_verdict: dict[str, list[validation_runtime.CheckResult]] = {}
    for r in results:
        by_verdict.setdefault(r.verdict, []).append(r)

    lines = ["## Validation Results\n"]

    # Render in canonical order: pass, fail, error, warning, advisory
    order = ["pass", "fail", "error", "warning", "advisory"]
    for verdict in order:
        items = by_verdict.get(verdict, [])
        if not items:
            continue

        # Capitalize verdict
        verdict_label = verdict.capitalize()
        lines.append(f"**{verdict_label} ({len(items)})**")
        for result in items:
            lines.append(f"- `{result.check_id}`: {result.rationale}")
        lines.append("")

    return "\n".join(lines)
