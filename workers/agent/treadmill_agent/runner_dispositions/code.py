"""``code`` disposition — diff → commit → push → PR (today's behavior).

Per ADR-0022, this is the original runner workflow extracted into a
handler so the dispatch table can route ``role-code-author`` to it.

Empty diff handling is workflow-aware (added 2026-05-13 to address
the wf-feedback empty-diff failure mode observed in the ADR-0023
smoke — see docs/handoffs/2026-05-13-adr-0023-smoke-and-validation-holes.md):

  * ``wf-author`` — empty diff is a failure (role was asked to make
    new code and didn't). Raises ``CodeAuthorError`` →
    ``step.failed`` via the runner's exception layer.
  * ``wf-feedback`` — empty diff is a legitimate
    ``responded-without-change`` decision per ADR-0012's value-set.
    The reviewer's nit may have been hallucinated, already
    addressed, or the analyzer may have produced a directive that's
    a no-op against the live tree. Failing here would orphan the
    PR in ``changes_requested`` with no path forward.
  * ``wf-ci-fix`` / ``wf-conflict`` — left strict (still raises).
    Their semantics for empty diff are murkier (``not-our-bug`` vs
    ``gave-up``) and need explicit role-prompt coupling before
    softening. Follow-up to the Ralph-loop ADR.

The decision string is ``pushed`` on success per ADR-0012's
``wf-author`` convention map.
"""

from __future__ import annotations

import logging
from typing import Any

from treadmill_agent import claude_code, git, validation_runtime
from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext

logger = logging.getLogger("treadmill.agent.code")


_SOFT_EMPTY_DIFF_WORKFLOWS: frozenset[str] = frozenset({"wf-feedback"})


def handle(ctx: DispositionContext) -> StepOutput:
    """Stage everything, fail on empty diff (real-Claude path),
    run validations, commit, push, open the PR, return the envelope.

    The dry-run path skips the empty-diff check because
    ``_dry_run_author`` always writes a marker file — that's what
    keeps the dry-run smoke green.

    Per the 2026-05-14 learning, validation scripts are executed before
    ``git push`` to gate on author-side self-validation. If any deterministic
    check fails, the step decision is ``fail``, output captures stderr, and
    the push is skipped.
    """
    git.stage_all(ctx.repo_dir)
    if not ctx.is_dry_run and not git.has_staged_changes(ctx.repo_dir):
        if ctx.ctx.workflow_id in _SOFT_EMPTY_DIFF_WORKFLOWS:
            payload: dict[str, Any] = {}
            if ctx.ctx.pr_number is not None:
                payload["pr_number"] = ctx.ctx.pr_number
            return StepOutput(
                summary=ctx.claude_result.summary,
                decision="responded-without-change",
                commit_sha=None,
                artifacts=[],
                payload=payload,
                metadata=Metadata(),
            )
        raise claude_code.CodeAuthorError(
            "Claude Code produced no changes to commit"
        )

    from treadmill_agent.runner import _commit_message, _is_analyzer_role  # local import to avoid cycle

    commit_sha = git.commit_all(ctx.repo_dir, _commit_message(ctx.ctx))

    # Run validation checks before pushing (per 2026-05-14 learning).
    # Only deterministic checks are run at author time; LLM-judge checks
    # are deferred to wf-validate post-merge.
    validation_result = _run_author_validations(ctx)
    if validation_result is not None:
        # Validation failed; return failure with output captured.
        return validation_result

    git.push_branch(ctx.repo_dir, ctx.branch)
    pr_number, pr_url = git.open_pr(
        repo_dir=ctx.repo_dir,
        branch=ctx.branch,
        title=ctx.ctx.title,
        body=ctx.claude_result.summary or ctx.ctx.title,
        repo=ctx.ctx.repo,
        mode=ctx.settings.repo_mode,
    )

    artifacts: list[Artifact] = [Artifact(kind="branch", value=ctx.branch)]
    if pr_url:
        artifacts.append(Artifact(kind="pr_url", value=pr_url))
    payload: dict[str, Any] = {}
    if pr_number is not None:
        payload["pr_number"] = pr_number
    # Dry-run analyzer extension (ADR-0015 §D.1): synthesize a minimal
    # ``task_directive`` so the downstream action step's
    # ``prior_steps[-1].output.payload.task_directive`` is non-empty
    # end-to-end. Production analyzers should emit ``output_kind=analysis``
    # and land in ``analysis.handle`` instead, but the dry-run path runs
    # the dry-run authoring marker (a code-like commit), so the
    # cross-step handoff still needs to fire here.
    if ctx.is_dry_run and _is_analyzer_role(ctx.ctx.role.id):
        from treadmill_agent.runner import _dry_run_task_directive  # local import

        payload["task_directive"] = _dry_run_task_directive(ctx.ctx)
    return StepOutput(
        summary=ctx.claude_result.summary,
        decision="pushed",
        commit_sha=commit_sha,
        artifacts=artifacts,
        payload=payload,
        metadata=Metadata(),
    )


def _run_author_validations(ctx: DispositionContext) -> StepOutput | None:
    """Run deterministic validation checks before pushing.

    Returns a failure StepOutput if any check fails; returns None if all
    checks pass. Per the 2026-05-14 learning, only blocking deterministic
    checks are run at author time. LLM-judge checks are deferred to
    wf-validate post-merge.

    Returns:
        StepOutput with decision='fail' and output containing validation
        failures, or None if all checks pass.
    """
    # Only run deterministic checks; LLM-judge checks are post-merge.
    deterministic_checks = [
        v for v in ctx.ctx.task_validations
        if v.kind == "deterministic" and v.script
    ]

    if not deterministic_checks:
        # No deterministic checks to run.
        return None

    results = []
    for check in deterministic_checks:
        result = validation_runtime.run_deterministic(
            check=_normalize_check(check),
            repo_dir=ctx.repo_dir,
            timeout_seconds=30,
        )
        results.append(result)

    # Aggregate: if any deterministic check fails, fail the step.
    # (All checks are blocking by definition at author time.)
    failures = [r for r in results if r.verdict == "fail"]
    if failures:
        summary = _compose_validation_failure_summary(results)
        payload: dict[str, Any] = {
            "validation_results": [
                {
                    "check_id": r.check_id,
                    "kind": r.kind,
                    "verdict": r.verdict,
                    "rationale": r.rationale,
                    "log_excerpt": r.log_excerpt,
                }
                for r in results
            ]
        }
        return StepOutput(
            summary=summary,
            decision="fail",
            commit_sha=None,
            artifacts=[],
            payload=payload,
            metadata=Metadata(),
        )

    # All checks passed.
    return None


def _normalize_check(validation_info: Any) -> Any:
    """Convert TaskValidationInfo to a check object for validation_runtime.

    validation_runtime expects an object with .id, .kind, .severity,
    .script attributes. TaskValidationInfo lacks .severity, so we add it.
    """

    class NormalizedCheck:
        pass

    check = NormalizedCheck()
    check.id = validation_info.id
    check.kind = validation_info.kind
    check.severity = "blocking"  # Author-time checks are always blocking.
    check.script = validation_info.script
    return check


def _compose_validation_failure_summary(
    results: list[validation_runtime.CheckResult],
) -> str:
    """Compose a human-readable summary of validation failures.

    Groups results by verdict and includes log excerpts for debugging.
    """
    by_verdict: dict[str, list[validation_runtime.CheckResult]] = {}
    for r in results:
        by_verdict.setdefault(r.verdict, []).append(r)

    lines = ["## Author-Side Validation Failed\n"]

    # Render failures first, then errors, then passes.
    order = ["fail", "error", "pass"]
    for verdict in order:
        items = by_verdict.get(verdict, [])
        if not items:
            continue

        verdict_label = verdict.capitalize()
        lines.append(f"**{verdict_label} ({len(items)})**")
        for result in items:
            lines.append(f"- `{result.check_id}`: {result.rationale}")
            if result.log_excerpt:
                lines.append(f"  ```\n{result.log_excerpt}\n  ```")
        lines.append("")

    return "\n".join(lines)
