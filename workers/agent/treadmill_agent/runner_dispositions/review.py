"""``review`` disposition — post a PR review via ``gh pr review``.

Per ADR-0022, the review-kind role's prompt instructs Claude to end
its output with ``VERDICT: approve | request_changes | comment``. The
handler greps for the last matching line; if none is found, the
verdict defaults to ``comment`` (the safe default — never accidentally
approves a PR Treadmill can't actually evaluate).

Empty diff is a SUCCESS for review-kind. The reviewer was asked to
look at code, not to modify it. The PR-side side effect (the posted
review) is the role's actual output.

Required context: ``pr_number`` must be present on the step context.
A review-kind step against a task that hasn't opened a PR yet is a
configuration error worth catching loudly (``MissingContextError``).
"""

from __future__ import annotations

import re

from treadmill_agent import gh
from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext


class MissingContextError(RuntimeError):
    """Raised when a per-kind handler needs a context field the runner
    didn't (or couldn't) populate — e.g. ``pr_number`` is required for
    a review-kind step but is ``None`` because the task hasn't opened
    a PR yet."""


_VERDICT_RE = re.compile(
    r"^\s*VERDICT:\s*(approve|request_changes|comment)\s*$",
    re.MULTILINE,
)


def _parse_verdict_marker(summary: str, *, default: str = "comment") -> str:
    """Return the last ``VERDICT: ...`` line's value, or ``default``.

    Per ADR-0022 Q22.c: if Claude is ambiguous (multiple VERDICT
    lines), the handler takes the *last* match. The role's prompt
    teaches a single-line marker convention; multiple lines is a
    prompt-engineering bug, not a runner bug.

    The default — ``comment`` — is the benign fallback so a missing /
    malformed marker never accidentally approves a PR.
    """
    matches = _VERDICT_RE.findall(summary or "")
    if not matches:
        return default
    return matches[-1]


def handle(ctx: DispositionContext) -> StepOutput:
    """Parse the verdict, post the review, return the envelope."""
    if ctx.ctx.pr_number is None:
        raise MissingContextError(
            f"review-kind step {ctx.ctx.step_id!r} requires pr_number but "
            "the task has no task_prs row; the task must open a PR before "
            "a review can be posted"
        )
    verdict = _parse_verdict_marker(ctx.claude_result.summary)
    # Dry-run path: skip the gh CLI invocation; tests assert the
    # parsed verdict + envelope shape without a live GitHub.
    if not ctx.is_dry_run:
        gh.pr_review(
            ctx.ctx.pr_number,
            verdict=verdict,  # type: ignore[arg-type]
            body=ctx.claude_result.summary,
            cwd=ctx.repo_dir,
        )
    return StepOutput(
        summary=ctx.claude_result.summary,
        # Map verdict → ADR-0012's wf-review decision value-set.
        decision=_DECISION_FOR_VERDICT[verdict],
        commit_sha=None,
        artifacts=[Artifact(kind="pr_review", value=verdict)],
        payload={"pr_number": ctx.ctx.pr_number, "verdict": verdict},
        metadata=Metadata(),
    )


# ADR-0012 §"Decision-string value-sets per workflow" for wf-review:
#   ``approved`` / ``changes_requested`` / ``needs-more-info``.
# The runner maps gh-CLI verb verdicts → decision values here so the
# downstream consumer + mergeability VIEW see the canonical strings.
_DECISION_FOR_VERDICT: dict[str, str] = {
    "approve": "approved",
    "request_changes": "changes_requested",
    "comment": "needs-more-info",
}
