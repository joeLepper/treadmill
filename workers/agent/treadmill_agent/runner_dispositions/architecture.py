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


def _extract_verdict_envelope(summary: str) -> dict[str, Any]:
    """Return the last JSON block whose parsed object contains
    ``"verdict"`` keyed at one of the four valid literals.

    Following ADR-0027's review-envelope pattern: the LAST block wins,
    so an architect that explored alternatives in earlier blocks can
    converge to a final verdict in the closing block.
    """
    envelope: dict[str, Any] | None = None
    for m in _JSON_BLOCK_RE.finditer(summary):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("verdict") in _VALID_VERDICTS:
            envelope = data
    if envelope is None:
        raise ArchitectVerdictParseError(
            "architect summary contained no JSON block with a valid "
            "``verdict`` field; expected one of: "
            + ", ".join(sorted(_VALID_VERDICTS))
        )
    return envelope


def _build_dispatch_payload(
    *,
    verdict: str,
    target_artifact: str,
    remediation_summary: str | None,
    task_id: str,
    rework_attempt: int,
) -> dict[str, Any]:
    """Build the routing payload the consumer reads to dispatch the
    downstream workflow. Shape per ADR-0032 §Decision."""
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


def handle(ctx: DispositionContext) -> StepOutput:
    """Parse the architect verdict envelope and emit the routing
    payload. No git or PR side effects — those happen downstream when
    the coordination consumer reads ``payload.dispatch`` and fires the
    next workflow.

    On parse failure (``ArchitectVerdictParseError``) propagates as a
    step failure; wf-feedback can re-run the architect with an explicit
    envelope reminder.
    """
    summary = ctx.claude_result.summary or ""
    envelope = _extract_verdict_envelope(summary)

    verdict: str = envelope["verdict"]
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
