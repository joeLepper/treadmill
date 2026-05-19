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

Per ADR-0049, the prior ``uncertain`` verdict was removed; the architect
must always commit to one of the three actionable verdicts above.

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

from pydantic import ValidationError as PydanticValidationError

from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext
from treadmill_api.events.validator_tuning import ValidatorTuning

logger = logging.getLogger("treadmill.agent.architecture")

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# ADR-0032 §Decision three-verdict contract (post-ADR-0049). Kept in
# sync with ``ArchitectVerdict.verdict`` Literal in
# ``services/api/treadmill_api/events/architect_verdict.py``.
_VALID_VERDICTS = frozenset({"amend", "supersede", "accept-as-is"})


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
#
# Per ADR-0049, ``uncertain`` was removed from the verdict surface; the
# cue table now covers only the three actionable verdicts.
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


def _parse_verdict_from_prose(summary: str) -> dict[str, Any] | None:
    """Fallback verdict parser. Scans prose for phrase cues and
    synthesizes a verdict envelope.

    Ordered fallback chain (post-ADR-0049):
      1. Try the cue table (amend → supersede → accept-as-is).
      2. If nothing matches, return ``None`` so the caller raises
         ``ArchitectVerdictParseError`` and the step.failure surfaces.
         Without ``uncertain`` as a catch-all, an unrecognized prose
         pattern is a hard failure rather than a silent rework-loop.

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
    return None


_RETRY_PROMPT = (
    "Below is your previous analysis as the Treadmill architect. "
    "Reformat your verdict as a single fenced JSON block — nothing "
    "else, no surrounding prose, no commentary. Use exactly these "
    "fields:\n"
    "```json\n"
    "{\n"
    '  "verdict": "amend" | "supersede" | "accept-as-is",\n'
    '  "reasoning": "<one paragraph distilling your prior analysis>",\n'
    '  "target_artifact": "<path to the implicated artifact>",\n'
    '  "remediation_summary": "<required for amend/supersede; omit for accept-as-is>"\n'
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
    hard-fail ``ArchitectVerdictParseError`` if no cue matches.

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
    ``"verdict"`` keyed at one of the three valid literals.

    Ordered chain (highest fidelity first, post-ADR-0049):
      1. Strict JSON parse from the original summary.
      2. Structured-output retry — ask claude to reformat its prose
         as a JSON envelope (when ``retry_model`` is supplied).
      3. Prose-cue parsing — pattern-match the summary for verdict
         phrasings.
      4. Hard fail — no cue matched (or summary is empty). Raises
         ``ArchitectVerdictParseError`` so wf-feedback can re-run the
         architect with an envelope reminder.

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
        # ADR-0038 + ADR-0042: when the architect was dispatched to
        # arbitrate a ralph-loop deadlock, ``accept-as-is`` means "the
        # work is fine; the gate was wrong." We emit BOTH overrides
        # because the deadlock predicate fires on either gate
        # (wf-review.changes_requested or wf-validate.fail) and the
        # architect's blanket accept-as-is waives whichever was the
        # blocker. Each override only takes effect in the mergeability
        # VIEW if the corresponding gate's latest signal at HEAD was a
        # fail — an override against an already-passing gate is harmless.
        if trigger == _DEADLOCK_TRIGGER:
            return {
                "workflow_id": None,
                "task_id": task_id,
                "review_override": True,
                "validate_override": True,
            }
        # ADR-0032 (Class C learning trigger): the original semantics
        # — append a pitfall to the component's AGENT.md.
        return {
            "workflow_id": "wf-doc-amend",
            "task_id": task_id,
            "target_artifact": target_artifact,
            "intent": "append-pitfall",
        }
    # Should be unreachable; _VALID_VERDICTS gates the parse.
    raise ArchitectVerdictParseError(f"unknown verdict: {verdict!r}")


def _build_pr_comment_payload(
    *,
    verdict: str,
    reasoning: str,
    target_artifact: str,
) -> dict[str, Any] | None:
    """Return the PR-comment routing hint per ADR-0033 §PR comments.

    Only ``accept-as-is`` surfaces a comment so the operator confirms
    the gap is acceptable; other verdicts route purely to downstream
    workflows. (Per ADR-0049, ``uncertain`` was removed from the verdict
    surface, so the prior capped-uncertain comment path is gone too.)
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

    # Empty-diff safety: only ``amend`` makes sense on a branch with no
    # commits against origin/main. ``accept-as-is`` is meaningless
    # (nothing to accept), and ``supersede`` is unrelated. Force amend
    # so the partnership (per ADR-0032 / ADR-0038, with #113 wiring
    # amend → wf-feedback) re-engages the author / feedback loop.
    if (
        verdict == "accept-as-is"
        and _branch_has_no_commits_against_main(ctx.repo_dir)
    ):
        logger.warning(
            "architect verdicted accept-as-is on a branch with no commits "
            "against origin/main — forcing verdict=amend (no work to "
            "accept). Architect's original prose: %r",
            (envelope.get("reasoning") or "")[:200],
        )
        original_verdict = verdict
        verdict = "amend"
        envelope["verdict"] = "amend"
        envelope["empty_diff_forced_amend"] = True
        envelope["remediation_summary"] = (
            f"The architect verdicted {original_verdict}, but the task's "
            "branch has no commits against origin/main — wf-author likely "
            "failed its author-side validation gate (PR #121) and never "
            "pushed. There is nothing to accept. Re-engage wf-feedback "
            "to author the missing work (likely test files referenced by "
            "the task's validation script). Original architect reasoning: "
            + (envelope.get("reasoning") or "<empty>")
        )
    reasoning: str = envelope.get("reasoning", "")
    target_artifact: str = envelope.get("target_artifact", "")
    remediation_summary: str | None = envelope.get("remediation_summary")

    dispatch_payload = _build_dispatch_payload(
        verdict=verdict,
        target_artifact=target_artifact,
        remediation_summary=remediation_summary,
        task_id=ctx.ctx.task_id,
        trigger=ctx.ctx.trigger,
    )
    pr_comment_payload = _build_pr_comment_payload(
        verdict=verdict,
        reasoning=reasoning,
        target_artifact=target_artifact,
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
    # Surface optional validator_tuning sub-object per ADR-0040.
    # Best-effort: a malformed tuning is dropped with a WARN log rather
    # than failing the step (the routing payload is still useful).
    raw_tuning = envelope.get("validator_tuning")
    if raw_tuning is not None:
        try:
            tuning = ValidatorTuning(**raw_tuning)
            payload["validator_tuning"] = tuning.model_dump(mode="json")
        except (PydanticValidationError, TypeError) as exc:
            logger.warning(
                "architect envelope carries validator_tuning but it failed "
                "validation — dropping. Error: %s",
                exc,
            )

    logger.info(
        "architect verdict=%s target=%s dispatch=%s",
        verdict, target_artifact, dispatch_payload.get("workflow_id"),
    )

    return StepOutput(
        summary=summary,
        decision=verdict,
        commit_sha=None,
        artifacts=[Artifact(kind="analysis", value=summary)],
        payload=payload,
        metadata=Metadata(),
    )
