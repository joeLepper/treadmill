"""``review`` disposition — post a PR comment carrying the verdict.

Per ADR-0022 + task #108 path 1, the review-kind role's prompt
instructs Claude to end its output with
``VERDICT: approve | request_changes | comment``. The handler greps
for the last matching line; if none is found, the verdict defaults to
``comment`` (the safe default — never accidentally approves a PR
Treadmill can't actually evaluate).

The handler posts the verdict as a ``gh pr comment`` body (not
``gh pr review``) — GitHub blocks same-author ``gh pr review`` calls,
and Treadmill's single-PAT identity authors and reviews under the
same user. The mergeability VIEW (ADR-0013) reads ``decision`` from
the Treadmill envelope rather than from GitHub's pr_review_submitted
event, so the formal-review state on the PR page is no longer
load-bearing. The companion change in
``coordination/triggers.py`` fires ``wf-feedback`` directly from a
``wf-review.step.completed`` event whose ``decision`` is
``changes_requested``, so the self-feedback loop survives the switch.

Empty diff is a SUCCESS for review-kind. The reviewer was asked to
look at code, not to modify it. The PR-side side effect (the posted
comment) is the role's human-facing output.

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


# Tourniquet for the markdown-drift bug observed on PR #10
# 2026-05-12 (the strict ``^VERDICT: ...$`` regex rejected
# ``**VERDICT: request_changes**`` — model emphasis defeated the parse,
# the verdict fell back to ``comment``, the mergeability VIEW collapsed
# that to ``blocked-on-review``, the runner re-authored, and the loop
# deathlooped). Per-line normalization strips the common Markdown
# decorations the model produces under emphasis instructions: leading
# list / blockquote markers, surrounding ``*`` / ``_`` / backtick
# wrapping, and trailing punctuation. The durable fix is a structured
# JSON envelope (ADR-0027); this widening is the tourniquet that lets
# the running loop survive until that lands.
_VERDICT_INNER_RE = re.compile(
    r"^VERDICT:\s*(approve|request_changes|comment)$",
)
_LEADING_MARKER_RE = re.compile(r"^\s*(?:[-*+>]\s+|>\s*)*")
_SURROUNDING_DECORATION_RE = re.compile(r"^[*_`]+|[*_`]+$")
_TRAILING_PUNCTUATION_RE = re.compile(r"[.,;:!?\s]+$")


def _normalize_verdict_line(line: str) -> str:
    """Strip the markdown decorations a model commonly adds around a
    marker line so the strict inner regex can still match.

    Operates on a single line. Returns the bare ``VERDICT: <value>``
    text if recognizable, else an empty string. Conservative: only
    peels decorations we've actually seen the model emit; does not
    fuzzy-match the verdict word itself (so e.g. ``VERDICT: lgtm``
    still fails, falling through to the safe default).
    """
    s = _LEADING_MARKER_RE.sub("", line).strip()
    # Repeatedly peel surrounding ``*``/``_``/backtick pairs; the model
    # occasionally double-wraps (``**`*VERDICT*`**``).
    while True:
        peeled = _SURROUNDING_DECORATION_RE.sub("", s).strip()
        if peeled == s:
            break
        s = peeled
    s = _TRAILING_PUNCTUATION_RE.sub("", s)
    return s


def _parse_verdict_marker(summary: str, *, default: str = "comment") -> str:
    """Return the last ``VERDICT: ...`` line's value, or ``default``.

    Per ADR-0022 Q22.c: if Claude is ambiguous (multiple VERDICT
    lines), the handler takes the *last* match. The role's prompt
    teaches a single-line marker convention; multiple lines is a
    prompt-engineering bug, not a runner bug.

    The default — ``comment`` — is the benign fallback so a missing /
    malformed marker never accidentally approves a PR.
    """
    last: str | None = None
    for line in (summary or "").splitlines():
        normalized = _normalize_verdict_line(line)
        m = _VERDICT_INNER_RE.match(normalized)
        if m is not None:
            last = m.group(1)
    return last if last is not None else default


_VERDICT_HEADER_VERB: dict[str, str] = {
    "approve": "approve",
    "request_changes": "request changes",
    "comment": "comment",
}


def _compose_comment_body(verdict: str, summary: str) -> str:
    """Prepend a verdict header to the model's review body so a human
    reader scanning the PR page sees the verdict immediately.

    The header is the *only* contract surface for a human reader; the
    runner side reads the verdict from the StepOutput envelope, not by
    re-parsing this body. Header text is deliberately a plain English
    verb (``approve`` / ``request changes`` / ``comment``) so it reads
    naturally above the review prose."""
    verb = _VERDICT_HEADER_VERB.get(verdict, verdict)
    return f"## Treadmill review verdict: {verb}\n\n{summary}"


def handle(ctx: DispositionContext) -> StepOutput:
    """Parse the verdict, post the comment, return the envelope."""
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
        gh.pr_comment(
            ctx.ctx.pr_number,
            body=_compose_comment_body(verdict, ctx.claude_result.summary),
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
