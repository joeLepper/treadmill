"""``review`` disposition — post a PR comment carrying the verdict.

Parser stack (ADR-0027 + task #108 path 1):

  1. **JSON envelope path (primary, ADR-0027).** The role-reviewer
     prompt instructs Claude to end its output with a fenced JSON
     block matching ``ReviewVerdict``. ``_parse_review_envelope``
     extracts the LAST ```` ```json ... ``` ```` block, parses it as
     JSON, and validates it against the Pydantic model. The verdict's
     closed value-set + the model's typed rationale field replace the
     prose-marker grep with a typed boundary (the pattern ADR-0011
     established for every other output kind).
  2. **Regex tourniquet (fallback, kept ~one release).** On JSON
     parse / validation failure (or no fence at all), the handler
     emits a structured ``review.json_parse_failed`` warning and
     falls through to the per-line ``VERDICT:`` regex parser. This
     keeps the loop alive during prompt-rollout drift; per Q27.a's
     resolution, the regex is deleted after 10 consecutive runs land
     cleanly via the JSON path.
  3. **Safe default (``comment``).** If both paths fail, the verdict
     defaults to ``comment`` — never accidentally approves a PR
     Treadmill can't actually evaluate.

Transport: ``gh pr comment`` per task #108 path 1. GitHub blocks
same-author ``gh pr review`` and Treadmill's single-PAT identity
authors AND reviews under the same user. The mergeability VIEW
(ADR-0013) reads ``decision`` from the Treadmill envelope, not from
GitHub's pr_review_submitted event, so the formal-review state on
the PR page is no longer load-bearing. The companion change in
``coordination/triggers.py`` fires ``wf-feedback`` directly from a
``wf-review.step.completed`` whose ``decision`` is
``changes_requested``, closing the self-feedback loop.

Empty diff is a SUCCESS for review-kind. The reviewer was asked to
look at code, not to modify it. The PR-side side effect (the posted
comment) is the role's human-facing output.

Required context: ``pr_number`` must be present on the step context.
A review-kind step against a task that hasn't opened a PR yet is a
configuration error worth catching loudly (``MissingContextError``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from treadmill_agent import gh, git
from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext

logger = logging.getLogger("treadmill.agent.review")


class ReviewVerdict(BaseModel):
    """Structured envelope for the review-kind role's terminal output.

    Replaces the prose ``VERDICT: ...`` marker with a Pydantic-typed
    boundary per ADR-0027. ``verdict`` is a closed ``Literal`` so
    Pydantic rejects anything outside the value-set;
    ``rationale`` is required + capped at 4000 chars per Q27.b
    (cheap insurance against a runaway model, ample for substantive
    rationale).
    """

    model_config = ConfigDict(extra="forbid")

    # ``comment`` was retired 2026-05-15: in a hands-free world the
    # reviewer must drive forward motion. Either the PR is good enough
    # to merge (``approve``) or it isn't (``request_changes``). An
    # ambiguous verdict yields an ambiguous downstream state.
    verdict: Literal["approve", "request_changes"]
    rationale: str = Field(..., max_length=4000)


# JSON fence regex per ADR-0027. Tolerates ``json``, ``json5``, mixed
# case (``JSON``, ``Json5``); rejects non-JSON fences (e.g. ```yaml).
# DOTALL so the body can span newlines; IGNORECASE for the lang tag.
_JSON_FENCE_RE = re.compile(
    r"```json5?\s*\n(.*?)\n```",
    flags=re.DOTALL | re.IGNORECASE,
)


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
    r"^VERDICT:\s*(approve|request_changes)$",
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


def _parse_verdict_marker(summary: str, *, default: str = "request_changes") -> str:
    """Return the last ``VERDICT: ...`` line's value, or ``default``.

    Per ADR-0022 Q22.c: if Claude is ambiguous (multiple VERDICT
    lines), the handler takes the *last* match. The role's prompt
    teaches a single-line marker convention; multiple lines is a
    prompt-engineering bug, not a runner bug.

    The default — ``request_changes`` — is conservative: a missing /
    malformed marker never accidentally approves a PR, and the
    request_changes verdict gives the system a productive next step
    (wf-feedback) instead of the legacy ``comment`` black hole.

    Per ADR-0027 this regex-driven path is the tourniquet fallback
    behind the JSON-envelope parser. Kept until 10 consecutive runs
    land cleanly via the JSON path (Q27.a).
    """
    last: str | None = None
    for line in (summary or "").splitlines():
        normalized = _normalize_verdict_line(line)
        m = _VERDICT_INNER_RE.match(normalized)
        if m is not None:
            last = m.group(1)
    return last if last is not None else default


def _extract_json_block(summary: str) -> str | None:
    """Return the contents of the LAST ```` ```json ... ``` ```` block
    in ``summary``, or ``None`` if no such block exists.

    Tolerates ``json``, ``json5``, mixed case in the fence language
    tag. Non-JSON fences (e.g. ```` ```yaml ```` blocks) are not
    matched — the regex's language-tag whitelist is the guard.
    """
    matches = _JSON_FENCE_RE.findall(summary or "")
    if not matches:
        return None
    return matches[-1]


def _strip_json_block(summary: str) -> str:
    """Remove the LAST JSON fence from ``summary``.

    Per Q27.c (strip without marker): the PR-page reader sees clean
    prose; the verdict's mergeability-VIEW effect is already the
    operator-visible signal so no in-body marker is needed.

    Splices out only the last match (not all matches) — if the model
    emits an earlier ``json`` fence for some legitimate reason
    (showing example data, etc.), that block stays in the body. The
    last-fence convention is the model's "terminal verdict" channel
    per ADR-0027.
    """
    text = summary or ""
    matches = list(_JSON_FENCE_RE.finditer(text))
    if not matches:
        return text
    last = matches[-1]
    return text[:last.start()] + text[last.end():]


def _parse_review_envelope(summary: str) -> tuple[str, str | None]:
    """Parse the review-kind output into ``(verdict, rationale)``.

    Three-tier parser per ADR-0027:

      1. **JSON envelope (primary).** ``_extract_json_block`` →
         ``json.loads`` → ``ReviewVerdict.model_validate``. On
         success, returns the typed verdict + rationale.
      2. **Regex tourniquet (fallback).** On ``JSONDecodeError`` /
         ``ValidationError``, emit a structured
         ``review.json_parse_failed`` warning and fall through to
         ``_parse_verdict_marker``. Returns ``(verdict, None)`` —
         the regex path can't recover a rationale.
      3. **Safe default.** If both paths fail, return
         ``("request_changes", None)`` — never silently approve.

    ``comment`` was retired 2026-05-15 (hands-free has no use for an
    ambiguous verdict). If the model emits ``"comment"`` in the JSON
    block, model_validate rejects it and the regex tourniquet returns
    the request_changes default.

    Per Q27.d's resolution, this function ALWAYS runs (dry-run path
    included); the dry-run only skips the ``gh pr comment`` call,
    not the parsing. The drift signal from the warning log is the
    point of the always-parse-always-log discipline.
    """
    block = _extract_json_block(summary)
    if block is not None:
        try:
            data = json.loads(block)
            parsed = ReviewVerdict.model_validate(data)
            return (parsed.verdict, parsed.rationale)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "review.json_parse_failed",
                extra={"reason": str(exc), "block_excerpt": block[:200]},
            )
    # Fallback: regex tourniquet. Returns ``request_changes`` if no
    # marker line matches.
    return (_parse_verdict_marker(summary), None)


def _has_verdict_marker(summary: str) -> bool:
    """Return True if the regex tourniquet found a literal VERDICT:
    line. No longer load-bearing for verdict resolution after the
    2026-05-15 comment-retirement (``request_changes`` is now the
    safe default whether or not a marker was present); retained as
    a hook for drift-signal tests in case the JSON-envelope path
    degrades."""
    for line in (summary or "").splitlines():
        normalized = _normalize_verdict_line(line)
        if _VERDICT_INNER_RE.match(normalized):
            return True
    return False


_VERDICT_HEADER_VERB: dict[str, str] = {
    "approve": "approve",
    "request_changes": "request changes",
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
    """Parse the verdict, post the comment, return the envelope.

    Per Q27.d: the parser runs unconditionally (incl. dry-run) so a
    structured drift warning surfaces even when no PR comment lands.
    Only ``gh pr comment`` itself is dry-run-gated.

    Per Q27.c: the JSON fence is stripped from the body before
    posting so the PR-page reader sees clean prose. The verdict's
    mergeability-VIEW effect is the operator-visible signal already.
    """
    if ctx.ctx.pr_number is None:
        raise MissingContextError(
            f"review-kind step {ctx.ctx.step_id!r} requires pr_number but "
            "the task has no task_prs row; the task must open a PR before "
            "a review can be posted"
        )
    summary = ctx.claude_result.summary
    verdict, rationale = _parse_review_envelope(summary)
    stripped_body = _strip_json_block(summary)
    # Dry-run path: skip the gh CLI invocation; tests assert the
    # parsed verdict + envelope shape without a live GitHub.
    if not ctx.is_dry_run:
        gh.pr_comment(
            ctx.ctx.pr_number,
            body=_compose_comment_body(verdict, stripped_body),
            cwd=ctx.repo_dir,
        )
    payload: dict[str, object] = {
        "pr_number": ctx.ctx.pr_number,
        "verdict": verdict,
    }
    if rationale is not None:
        payload["rationale"] = rationale
    # ADR-0013 mergeability VIEW joins wf-review steps on
    # commit_sha = head.head_sha. Without this, ``approved`` verdicts
    # never reach the VIEW and auto-merge never sees them.
    review_sha = git.head_sha(ctx.repo_dir)
    return StepOutput(
        summary=summary,
        # Map verdict → ADR-0012's wf-review decision value-set.
        decision=_DECISION_FOR_VERDICT[verdict],
        commit_sha=review_sha,
        artifacts=[Artifact(kind="pr_review", value=verdict)],
        payload=payload,
        metadata=Metadata(),
    )


# ADR-0012 §"Decision-string value-sets per workflow" for wf-review:
#   ``approved`` / ``changes_requested`` / ``needs-more-info``.
# The runner maps gh-CLI verb verdicts → decision values here so the
# downstream consumer + mergeability VIEW see the canonical strings.
_DECISION_FOR_VERDICT: dict[str, str] = {
    "approve": "approved",
    "request_changes": "changes_requested",
}
