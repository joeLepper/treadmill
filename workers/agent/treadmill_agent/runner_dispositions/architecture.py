"""``analysis`` disposition variant — architect verdict routing.

Per ADR-0032 §wf-architecture-resolve, the architect role returns an
``ArchitectVerdict`` envelope (ADR-0027 pattern, schema at
``services/api/treadmill_api/events/architect_verdict.py``). This handler
parses that envelope from the Claude summary, surfaces the routing
payload, and emits the downstream-dispatch hint for the coordination
consumer.

Routing per ADR-0032 §Decision:

* ``amend`` — intent right, code wrong. Payload carries
  ``dispatch.workflow_id = "wf-plan"`` so a remediation plan gets
  authored. ``target_artifact`` + ``remediation_summary`` flow through.
* ``supersede`` — intent no longer right. Payload carries
  ``dispatch.workflow_id = "wf-doc-amend"`` so a superseding ADR is
  authored at ``docs/adrs/<next>-*.md``.
* ``accept-as-is`` — gap is acceptable. Payload carries
  ``dispatch.workflow_id = "wf-doc-amend"`` against the component's
  ``AGENT.md`` (append to Pitfalls). Also emits a structured PR comment
  request (``pr_comment`` payload field) so the operator confirms.
* ``uncertain`` — block, re-dispatch. Payload carries
  ``dispatch.workflow_id = "wf-architecture-resolve"`` for rework.
  ``rework_attempt`` counts up; on the 5th, the consumer caps + leaves
  a PR comment (per ADR-0029 Q29.e, ADR-0032 Q32.e) instead of
  re-dispatching.

No git side effects. No PR-side side effects (the coordination consumer
emits the PR comment via the ``pr_comment`` helper from ADR-0033).

Empty diff is a SUCCESS — the architect role isn't asked to modify code.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any

from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext

logger = logging.getLogger("treadmill.agent.architecture")

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# Per ADR-0029 Q29.e / ADR-0032 Q32.e: 5-attempt rework cap.
MAX_REWORK_ATTEMPTS = 5

# ADR-0032 §Decision four-verdict contract. Kept in sync with
# ``ArchitectVerdict.verdict`` Literal in
# ``services/api/treadmill_api/events/architect_verdict.py``.
_VALID_VERDICTS = frozenset({"amend", "supersede", "accept-as-is", "uncertain"})


class ArchitectVerdictParseError(RuntimeError):
    """Raised when Claude's summary doesn't carry a parsable
    ArchitectVerdict envelope. The runner treats this as a step
    failure; wf-feedback against this task can re-run the architect
    with an explicit reminder to emit the envelope."""


# Prose-fallback verdict cues. Ordered by precedence: the disposition
# scans the model's summary for these phrases (lowercased) and assigns
# the matching verdict. ``accept-as-is`` listed last so phrases like
# "the work is complete; no amendment needed" don't fire ``amend``
# before the "accept" check has a chance.
#
# Observed 2026-05-15: sonnet on the role-architect prompt frequently
# produces a thorough prose verdict (e.g. "The implementation is
# already complete. The recent commit X delivered everything the task
# requires.") but omits the JSON envelope at the close — even after the
# prompt's closing imperative. The strict parser raised
# ``ArchitectVerdictParseError`` and the step.failed, burning attempts.
# This fallback extracts the model's intended verdict from prose so the
# system can act on it; the strict JSON path remains primary.
_PROSE_VERDICT_CUES: list[tuple[str, tuple[str, ...]]] = [
    ("amend", (
        "verdict: amend",
        "amend the implementation",
        "needs amendment",
        "remediation plan",
        "implementation is incomplete",
        "the code needs fixing",
        "the work is incomplete",
    )),
    ("supersede", (
        "verdict: supersede",
        "supersede the adr",
        "supersede the plan",
        "intent is no longer right",
        "intent has shifted",
    )),
    ("uncertain", (
        "verdict: uncertain",
        "need more context",
        "cannot decide",
        "ambiguous",
        "insufficient evidence",
    )),
    ("accept-as-is", (
        "verdict: accept-as-is",
        "accept as is",
        "accept-as-is",
        "implementation is already complete",
        "implementation is complete",
        "the work is already complete",
        "the work is complete",
        "no issues found",
        "no amendment needed",
        "no changes required",
        "all task requirements are implemented",
        "all changes are in place",
        "changes are in place",
        "the implementation matches",
        "implementation matches the spec",
        "everything the task requires",
        "everything the spec requires",
        "everything required by the spec",
        "the work satisfies the spec",
        "the diff covers everything",
    )),
]


# Last-resort prose cues — if NOTHING matches, prefer `uncertain` over
# raising. Uncertain re-dispatches the architect (up to ADR-0029 Q29.e's
# 5-attempt cap), which surfaces to operator at the cap. Better than
# dead-ending the task on an unrecognized prose pattern.
_UNCERTAIN_FALLBACK_CUE = (
    "(no recognized cue — defaulted to uncertain to keep loop moving)"
)


def _parse_verdict_from_prose(summary: str) -> dict[str, Any] | None:
    """Fallback verdict parser. Scans prose for phrase cues and
    synthesizes a verdict envelope.

    Ordered fallback chain:
      1. Try the cue table (amend → supersede → uncertain → accept-as-is).
      2. If nothing matches AND the summary has substantive content (not
         empty / not just whitespace), default to ``uncertain`` so the
         architect re-dispatches up to ADR-0029 Q29.e's 5-attempt cap.
         Operator surfaces at the cap. Better than dead-ending on an
         unrecognized prose pattern.
      3. If the summary is empty / blank, return ``None`` so the
         strict-parse error fires (this is the "model returned nothing
         useful" path — worth surfacing as a hard failure).

    The synthesized envelope marks ``parsed_from_prose: true`` so the
    dispatched downstream knows this verdict came from the lossy path
    and the upstream prompt or model should be tightened — but the
    system makes forward progress instead of dead-ending the task.
    """
    lower = summary.lower()
    for verdict, cues in _PROSE_VERDICT_CUES:
        for cue in cues:
            if cue in lower:
                return {
                    "verdict": verdict,
                    "reasoning": (
                        "Extracted from architect prose (no JSON envelope "
                        f"emitted). Matched cue: {cue!r}."
                    ),
                    "target_artifact": "",
                    "parsed_from_prose": True,
                }
    # No specific cue matched but the model did produce substantive
    # prose — default to uncertain to keep the loop moving.
    if summary.strip():
        return {
            "verdict": "uncertain",
            "reasoning": (
                "Architect produced prose but no recognized verdict cue "
                "matched; defaulted to ``uncertain`` so the task continues "
                "through the rework-cap path rather than dead-ending. "
                "Prompt or model may need tightening if this fires often."
            ),
            "target_artifact": "",
            "parsed_from_prose": True,
        }
    return None


_RETRY_PROMPT = (
    "Below is your previous analysis as the Treadmill architect. "
    "Reformat your verdict as a single fenced JSON block — nothing "
    "else, no surrounding prose, no commentary. Use exactly these "
    "fields:\n"
    "```json\n"
    "{\n"
    '  "verdict": "amend" | "supersede" | "accept-as-is" | "uncertain",\n'
    '  "reasoning": "<one paragraph distilling your prior analysis>",\n'
    '  "target_artifact": "<path to the implicated artifact>",\n'
    '  "remediation_summary": "<required for amend/supersede; omit for accept-as-is/uncertain>"\n'
    "}\n"
    "```\n\n"
    "Reply with ONLY the fenced ```json block. No other text.\n\n"
    "Previous analysis:\n"
    "```\n"
    "{prose}\n"
    "```\n"
)


def _try_structured_retry(
    summary: str, model: str, log_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Re-prompt claude with a focused JSON-only extraction prompt.

    Observed 2026-05-15→16: sonnet's architect often emits a usable
    prose verdict but skips the JSON envelope at the close. Rather
    than guess phrasings (the prose-cue path) or dead-end (the
    pre-fallback behavior), we make one short follow-up Claude call
    that ONLY asks for the structured envelope. This is higher
    fidelity than cue-matching: the model gets to choose the verdict
    explicitly instead of being guessed from prose.

    Returns the parsed envelope on success, ``None`` on any failure
    (claude unavailable, output un-parseable, model still produces
    prose). Failures fall through to the prose-cue path, then the
    uncertain catch-all. Keep the loop moving regardless.

    Cost: one Claude call (~$0.05–0.10 on sonnet, ~5–30s). Only
    fires when the strict JSON path failed.
    """
    binary = _find_claude_binary()
    if binary is None:
        return None
    prompt = _RETRY_PROMPT.replace("{prose}", summary)
    try:
        result = subprocess.run(
            [
                binary, "--print",
                "--output-format", "json",
                "--model", model,
                "--permission-mode", "acceptEdits",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=180,  # 3 min cap — focused call, should be fast
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning(
            "structured-output retry: claude invocation failed: %s; "
            "falling through to prose cues",
            exc,
        )
        return None
    if result.returncode != 0:
        logger.warning(
            "structured-output retry: claude exited %d; stderr=%r",
            result.returncode, result.stderr[:200],
        )
        return None
    # claude --output-format json emits {"type":"result", ..., "result": "<text>"}
    try:
        cli_payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning(
            "structured-output retry: claude stdout was not JSON: %r",
            result.stdout[:200],
        )
        return None
    retry_summary = cli_payload.get("result", "")
    if not retry_summary:
        return None
    # Scan retry_summary for the JSON envelope using the same strict
    # path as the primary parser.
    for m in _JSON_BLOCK_RE.finditer(retry_summary):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("verdict") in _VALID_VERDICTS:
            data["parsed_via_retry"] = True
            logger.info(
                "structured-output retry: extracted verdict %r",
                data.get("verdict"),
            )
            return data
    logger.warning(
        "structured-output retry: claude returned prose again "
        "(%d chars); falling through to prose cues",
        len(retry_summary),
    )
    return None


def _find_claude_binary() -> str | None:
    """Locate the ``claude`` CLI. Mirrors ``claude_code._find_binary``
    but without raising — we want graceful fallback if the worker
    image somehow lacks it."""
    import shutil
    return shutil.which("claude")


def _extract_verdict_envelope(
    summary: str, *, retry_model: str | None = None,
) -> dict[str, Any]:
    """Return the last JSON block whose parsed object contains
    ``"verdict"`` keyed at one of the four valid literals.

    Ordered chain (highest fidelity first):
      1. Strict JSON parse from the original summary.
      2. Structured-output retry — ask claude to reformat its prose
         as a JSON envelope (when ``retry_model`` is supplied).
      3. Prose-cue parsing — pattern-match the summary for verdict
         phrasings.
      4. Uncertain catch-all — if substantive prose exists but no
         signal extracted, default to ``uncertain`` so the loop
         continues through the rework-cap path.
      5. Hard fail (empty summary only).

    Each step has lower fidelity but keeps things moving. The retry
    closes the most common gap (sonnet skipping the JSON close)
    without guessing phrasings.
    """
    envelope: dict[str, Any] | None = None
    for m in _JSON_BLOCK_RE.finditer(summary):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("verdict") in _VALID_VERDICTS:
            envelope = data
    if envelope is not None:
        return envelope
    # Structured retry before prose-cue fallback.
    if retry_model:
        retry_envelope = _try_structured_retry(summary, retry_model)
        if retry_envelope is not None:
            return retry_envelope
    fallback = _parse_verdict_from_prose(summary)
    if fallback is not None:
        return fallback
    raise ArchitectVerdictParseError(
        "architect summary contained no JSON block with a valid "
        "``verdict`` field AND no prose cue matched; expected one of: "
        + ", ".join(sorted(_VALID_VERDICTS))
    )


_DEADLOCK_TRIGGER = "self:wf-feedback-deadlock"


def _build_dispatch_payload(
    *,
    verdict: str,
    target_artifact: str,
    remediation_summary: str | None,
    task_id: str,
    rework_attempt: int,
    trigger: str,
) -> dict[str, Any]:
    """Build the routing payload the consumer reads to dispatch the
    downstream workflow. Shape per ADR-0032 §Decision + ADR-0038
    semantics for deadlock-triggered runs."""
    if verdict == "amend":
        return {
            "workflow_id": "wf-plan",
            "task_id": task_id,
            "target_artifact": target_artifact,
            "remediation_summary": remediation_summary or "",
        }
    if verdict == "supersede":
        return {
            "workflow_id": "wf-doc-amend",
            "task_id": task_id,
            "target_artifact": target_artifact,
            "remediation_summary": remediation_summary or "",
            "intent": "author-superseding-adr",
        }
    if verdict == "accept-as-is":
        # ADR-0038: when the architect was dispatched to arbitrate a
        # ralph-loop deadlock, ``accept-as-is`` means "the work is fine;
        # the reviewer was wrong." Skip wf-doc-amend (no pitfall to
        # append) and let the consumer emit a ``review.override`` event
        # so the mergeability VIEW projects ``review_decision=approved``.
        if trigger == _DEADLOCK_TRIGGER:
            return {
                "workflow_id": None,
                "task_id": task_id,
                "review_override": True,
            }
        # ADR-0032 (Class C learning trigger): the original semantics
        # — append a pitfall to the component's AGENT.md.
        return {
            "workflow_id": "wf-doc-amend",
            "task_id": task_id,
            "target_artifact": target_artifact,
            "intent": "append-pitfall",
        }
    if verdict == "uncertain":
        if rework_attempt >= MAX_REWORK_ATTEMPTS:
            return {
                "workflow_id": None,
                "task_id": task_id,
                "capped": True,
                "rework_attempt": rework_attempt,
            }
        return {
            "workflow_id": "wf-architecture-resolve",
            "task_id": task_id,
            "rework_attempt": rework_attempt + 1,
        }
    # Should be unreachable; _VALID_VERDICTS gates the parse.
    raise ArchitectVerdictParseError(f"unknown verdict: {verdict!r}")


def _build_pr_comment_payload(
    *,
    verdict: str,
    reasoning: str,
    target_artifact: str,
    capped: bool,
) -> dict[str, Any] | None:
    """Return the PR-comment routing hint per ADR-0033 §PR comments.

    Only ``accept-as-is`` and capped ``uncertain`` surface a comment;
    other verdicts route purely to downstream workflows.
    """
    if verdict == "accept-as-is":
        return {
            "workflow_id": "wf-architecture-resolve",
            "signal": "accept-as-is",
            "summary": (
                f"Architect verdict: accept-as-is for "
                f"``{target_artifact}``.\n\n"
                f"Reasoning: {reasoning}"
            ),
            "action_items": (
                "- Confirm the gap is acceptable.\n"
                "- If you disagree, re-open the learning and re-dispatch "
                "``wf-architecture-resolve``."
            ),
            "see": f"See: ``{target_artifact}`` AGENT.md Pitfalls.",
        }
    if verdict == "uncertain" and capped:
        return {
            "workflow_id": "wf-architecture-resolve",
            "signal": "capped",
            "summary": (
                f"Architect could not converge after {MAX_REWORK_ATTEMPTS} "
                f"attempts on ``{target_artifact}``.\n\n"
                f"Last reasoning: {reasoning}"
            ),
            "action_items": (
                "- Operator judgment required.\n"
                "- Read the architect's latest reasoning above + the "
                "underlying learning, then decide manually: amend, "
                "supersede, or accept-as-is."
            ),
            "see": f"See: ``{target_artifact}``.",
        }
    return None


def _branch_has_no_commits_against_main(repo_dir: Any) -> bool:
    """Return True if the worker's checkout has no commits ahead of
    origin/main — i.e. the branch is empty (nothing to accept).

    Observed 2026-05-15→16 on tasks ``2a3eaadb``, ``b25b3f5d``,
    ``472e3ddc``, ``2850d0cd``: wf-author failed author-side validation
    (pytest exit 4 — no tests collected) so nothing was committed; the
    architect dispatched against the same task ran in an empty
    workspace and verdicted ``accept-as-is`` from prose like "all
    changes look fine" or "no issues found" — but there was literally
    no diff to accept. The ``review.override`` event then fires
    meaninglessly because the gate's target SHA matches origin/main.

    Returns ``False`` on git-command failure (we'd rather over-accept
    than spuriously force amend on a real diff that the command
    couldn't reach).
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) == 0
    except ValueError:
        return False


def handle(ctx: DispositionContext) -> StepOutput:
    """Parse the architect verdict envelope and emit the routing
    payload. No git or PR side effects — those happen downstream when
    the coordination consumer reads ``payload.dispatch`` and fires the
    next workflow.

    On parse failure (``ArchitectVerdictParseError``) propagates as a
    step failure; wf-feedback can re-run the architect with an explicit
    envelope reminder.

    Post-parse safety check: if the architect verdicted
    ``accept-as-is`` but the workspace has no commits against
    origin/main (the branch is empty — wf-author failed pre-push), the
    verdict is forcibly downgraded to ``amend`` with a synthetic
    remediation_summary explaining that nothing exists to accept and
    that wf-feedback should re-engage to author the work. Prevents
    review.override from firing meaninglessly.
    """
    summary = ctx.claude_result.summary or ""
    # Pass the role's model so the structured-output retry can use the
    # same model that produced the prose. Sonnet's prose is sonnet's to
    # convert; haiku's is haiku's.
    envelope = _extract_verdict_envelope(
        summary, retry_model=ctx.ctx.role.model,
    )

    verdict: str = envelope["verdict"]

    # Empty-diff safety: accept-as-is is meaningless when no work
    # exists to accept. Force amend so the partnership (per ADR-0032
    # / ADR-0038, with #113 wiring amend → wf-feedback) re-engages the
    # author/feedback loop instead of pretending the work is done.
    if (
        verdict == "accept-as-is"
        and _branch_has_no_commits_against_main(ctx.repo_dir)
    ):
        logger.warning(
            "architect verdicted accept-as-is on a branch with no commits "
            "against origin/main — forcing verdict=amend (no work to accept). "
            "Architect's original prose: %r",
            (envelope.get("reasoning") or "")[:200],
        )
        verdict = "amend"
        envelope["verdict"] = "amend"
        envelope["empty_diff_forced_amend"] = True
        envelope["remediation_summary"] = (
            "The architect verdicted accept-as-is, but the task's branch has "
            "no commits against origin/main — wf-author likely failed its "
            "author-side validation gate (PR #121) and never pushed. There is "
            "nothing to accept. Re-engage wf-feedback to author the missing "
            "work (likely test files referenced by the task's validation "
            "script). Original architect reasoning: "
            + (envelope.get("reasoning") or "<empty>")
        )
    reasoning: str = envelope.get("reasoning", "")
    target_artifact: str = envelope.get("target_artifact", "")
    remediation_summary: str | None = envelope.get("remediation_summary")
    # ``rework_attempt`` accumulates across re-dispatches of the same
    # task. Source is the upstream payload (the consumer increments on
    # each dispatch); v0 reads from envelope as a fallback.
    rework_attempt: int = int(envelope.get("rework_attempt", 0))

    dispatch_payload = _build_dispatch_payload(
        verdict=verdict,
        target_artifact=target_artifact,
        remediation_summary=remediation_summary,
        task_id=ctx.ctx.task_id,
        rework_attempt=rework_attempt,
        trigger=ctx.ctx.trigger,
    )
    capped = bool(dispatch_payload.get("capped"))
    pr_comment_payload = _build_pr_comment_payload(
        verdict=verdict,
        reasoning=reasoning,
        target_artifact=target_artifact,
        capped=capped,
    )

    payload: dict[str, Any] = {
        "verdict": verdict,
        "reasoning": reasoning,
        "target_artifact": target_artifact,
        "dispatch": dispatch_payload,
    }
    if remediation_summary:
        payload["remediation_summary"] = remediation_summary
    if pr_comment_payload is not None:
        payload["pr_comment"] = pr_comment_payload
    # Surface the prose-fallback marker so downstream telemetry can
    # track how often the strict-JSON path is missed.
    if envelope.get("parsed_from_prose"):
        payload["parsed_from_prose"] = True
    if envelope.get("parsed_via_retry"):
        payload["parsed_via_retry"] = True
    if envelope.get("empty_diff_forced_amend"):
        payload["empty_diff_forced_amend"] = True

    logger.info(
        "architect verdict=%s target=%s dispatch=%s capped=%s",
        verdict, target_artifact, dispatch_payload.get("workflow_id"),
        capped,
    )

    return StepOutput(
        summary=summary,
        decision=verdict,
        commit_sha=None,
        artifacts=[Artifact(kind="analysis", value=summary)],
        payload=payload,
        metadata=Metadata(),
    )
