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

from typing import Any

from treadmill_agent import claude_code, git
from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext


_SOFT_EMPTY_DIFF_WORKFLOWS: frozenset[str] = frozenset({"wf-feedback"})


def handle(ctx: DispositionContext) -> StepOutput:
    """Stage everything, fail on empty diff (real-Claude path),
    commit, push, open the PR, return the envelope.

    The dry-run path skips the empty-diff check because
    ``_dry_run_author`` always writes a marker file — that's what
    keeps the dry-run smoke green.
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
