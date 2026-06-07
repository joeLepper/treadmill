"""``analysis`` disposition variant — architect verdict routing.

Per ADR-0032 §wf-architecture-resolve, the architect role returns an
``ArchitectVerdict`` envelope (ADR-0027 pattern, schema at
``services/api/treadmill_api/events/architect_verdict.py``). This handler
parses that envelope from the Claude summary, surfaces the routing
payload, and emits the downstream-dispatch hint for the coordination
consumer.

Routing per ADR-0032 §Decision + ADR-0048 §supersede repurpose:

* ``amend`` — intent right, code wrong. Payload carries
  ``dispatch.workflow_id = "wf-plan"`` so a remediation plan gets
  authored. ``target_artifact`` + ``remediation_summary`` flow through.
* ``supersede`` — the plan-text itself was wrong. Payload carries
  the ``rewritten_description`` the architect emitted; the API-side
  ``maybe_dispatch_supersede_on_architect_verdict`` trigger reads it,
  closes the existing PR, creates a child task with the rewritten
  description + ``parent_task_id`` pointing back, and dispatches a
  fresh ``wf-author`` against the child. ``dispatch.workflow_id`` is
  ``None`` — the API trigger owns the dispatch shape, not the
  disposition. Per ADR-0048, the prior supersede→wf-doc-amend routing
  (author a superseding ADR) was removed; supersede now means
  "rewrite the task text and restart fresh" instead.
* ``accept-as-is`` — gap is acceptable. Payload carries
  ``dispatch.workflow_id = "wf-doc-amend"`` against the component's
  ``AGENT.md`` (append to Pitfalls). Also emits a structured PR comment
  request (``pr_comment`` payload field) so the operator confirms.

Per ADR-0048, the prior ``uncertain`` verdict was removed; the architect
must always commit to one of the three actionable verdicts above.

No git side effects. No PR-side side effects (the coordination consumer
emits the PR comment via the ``pr_comment`` helper from ADR-0033; the
supersede PR-close + child-task-create side effects fire from the
API-side trigger, not from this handler).

Empty diff is a SUCCESS — the architect role isn't asked to modify code.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext
from treadmill_api.events.validator_tuning import ValidatorTuning

logger = logging.getLogger("treadmill.agent.architecture")

# ADR-0032 §Decision four-verdict contract (post-ADR-0048 + ADR-0058).
# Kept in sync with ``ArchitectVerdict.verdict`` Literal in
# ``services/api/treadmill_api/events/architect_verdict.py``.
_VALID_VERDICTS = frozenset({"amend", "supersede", "accept-as-is", "gate-broken"})


class ArchitectVerdictParseError(RuntimeError):
    """Raised when Claude's summary doesn't carry a parsable
    ArchitectVerdict envelope. The runner treats this as a step
    failure; wf-feedback against this task can re-run the architect
    with an explicit reminder to emit the envelope."""


# ADR-0083: the verdict JSON Schema passed to ``claude --json-schema``
# when the role is role-architect (see runner.py for the call-site
# hookup, claude_code.py for the argv threading). Flat shape — the
# Anthropic tool-schema validator (which backs ``--json-schema``)
# rejects JSON Schema's ``allOf`` / ``oneOf`` / ``if-then-else``
# (verified 2026-06-07 smoke; CLI returned ``400 input_schema does not
# support oneOf``), so conditional-required fields for ``supersede``
# (``rewritten_description``) and ``gate-broken`` (``gate_log_excerpt``)
# are validated in ``_extract_verdict_envelope`` as a post-emit check
# instead of being enforced by the schema.
_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["amend", "supersede", "accept-as-is", "gate-broken"],
        },
        "reasoning": {"type": "string"},
        "target_artifact": {"type": "string"},
        "remediation_summary": {"type": "string"},
        "rewritten_description": {"type": "string"},
        "gate_log_excerpt": {"type": "string"},
    },
    "required": ["verdict", "reasoning", "target_artifact"],
}


# Discriminators for ``task.architect_emit_failure`` events the worker
# POSTs when the architect's structured emit either didn't happen or
# failed a worker-side post-validate. Matched on the API-side
# pydantic Literal in ``services/api/treadmill_api/events/task.py``.
_PARSE_FAILURE_NO_STRUCTURED = "no-structured-output"
_PARSE_FAILURE_INVALID_VERDICT = "invalid-verdict-literal"
_PARSE_FAILURE_SUPERSEDE_NO_REWRITE = "supersede-missing-rewrite"
_PARSE_FAILURE_GATE_BROKEN_NO_EXCERPT = "gate-broken-missing-excerpt"


def _extract_verdict_envelope(
    structured_output: dict[str, Any] | None,
    fallback_summary: str,
) -> dict[str, Any]:
    """Read the architect's verdict from the CLI's structured_output
    field (ADR-0083). Returns either a valid envelope or a synthetic
    ``{"verdict": "emit-failure", ...}`` envelope that ``handle()``
    recognizes as a no-dispatch escalation case.

    Four failure paths, all routed through the same synthetic emit-failure
    envelope:
      - structured_output absent → ``no-structured-output``
      - verdict literal not in the enum → ``invalid-verdict-literal``
        (defensive — the CLI enforces enum at the schema layer, but the
        worker stays paranoid)
      - verdict=supersede with empty rewritten_description →
        ``supersede-missing-rewrite``
      - verdict=gate-broken with empty gate_log_excerpt →
        ``gate-broken-missing-excerpt``

    No prose-fallback chain — that machinery (``_try_structured_retry``,
    ``_PROSE_VERDICT_CUES``, ``_parse_verdict_from_prose``) was deleted
    when ADR-0083 landed. The wedge is the schema constraint at the
    CLI; the residual misses route via emit-failure → cc-relay to the
    dispatching orchestrator session (NOT to the human).
    """
    if structured_output is None:
        return _emit_failure_envelope(
            reason=_PARSE_FAILURE_NO_STRUCTURED,
            model_output_excerpt=fallback_summary[:2048],
        )
    verdict = structured_output.get("verdict")
    if verdict not in _VALID_VERDICTS:
        return _emit_failure_envelope(
            reason=_PARSE_FAILURE_INVALID_VERDICT,
            model_output_excerpt=json.dumps(structured_output)[:2048],
        )
    if verdict == "supersede":
        rewritten = structured_output.get("rewritten_description") or ""
        if not rewritten.strip():
            return _emit_failure_envelope(
                reason=_PARSE_FAILURE_SUPERSEDE_NO_REWRITE,
                model_output_excerpt=json.dumps(structured_output)[:2048],
            )
    if verdict == "gate-broken":
        excerpt = structured_output.get("gate_log_excerpt") or ""
        if not excerpt.strip():
            return _emit_failure_envelope(
                reason=_PARSE_FAILURE_GATE_BROKEN_NO_EXCERPT,
                model_output_excerpt=json.dumps(structured_output)[:2048],
            )
    return structured_output


def _emit_failure_envelope(
    *, reason: str, model_output_excerpt: str,
) -> dict[str, Any]:
    """Synthetic envelope returned by ``_extract_verdict_envelope`` for
    each of the four parse-failure paths. ``handle()`` recognizes
    ``verdict='emit-failure'`` and routes the failure via cc-relay
    (POST in handle()) instead of dispatching a downstream workflow."""
    return {
        "verdict": "emit-failure",
        "parse_failure_reason": reason,
        "model_output_excerpt": model_output_excerpt,
    }


def _post_architect_emit_failure(
    *,
    api_base_url: str,
    api_timeout: float,
    task_id: str,
    failing_run_id: str,
    created_by: str,
    parse_failure_reason: str,
    model_output_excerpt: str,
) -> None:
    """POST a ``task.architect_emit_failure`` event to the API per
    ADR-0083. Hits the dedicated endpoint Task B (PR #243) defined —
    same operator_hint_set + worker_hint_request convention.

    Side-effect-only: failures are logged but not raised so a flaky
    POST does not turn into a step failure on top of an already-failed
    architect emit. The relay drop (Task B's trigger) is the durable
    escalation; this POST is the trigger source."""
    import httpx

    # Body shape mirrors ArchitectEmitFailureRequest in
    # services/api/treadmill_api/routers/tasks.py (Task B).
    body = {
        "parse_failure_reason": parse_failure_reason,
        "model_output_excerpt": model_output_excerpt[:4096],
        "created_by": created_by,
        "failing_run_id": failing_run_id,
    }
    url = f"{api_base_url}/api/v1/tasks/{task_id}/architect_emit_failure"
    try:
        with httpx.Client(timeout=api_timeout) as client:
            response = client.post(url, json=body)
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — side-effect best-effort
        logger.warning(
            "architect_emit_failure POST failed (non-fatal): %s", exc,
        )


_DEADLOCK_TRIGGER = "self:wf-feedback-deadlock"


def _build_dispatch_payload(
    *,
    verdict: str,
    target_artifact: str,
    remediation_summary: str | None,
    rewritten_description: str | None,
    task_id: str,
    trigger: str,
) -> dict[str, Any]:
    """Build the routing payload the consumer reads to dispatch the
    downstream workflow. Shape per ADR-0032 §Decision + ADR-0038
    semantics for deadlock-triggered runs + ADR-0048 supersede repurpose."""
    if verdict == "amend":
        return {
            "workflow_id": "wf-plan",
            "task_id": task_id,
            "target_artifact": target_artifact,
            "remediation_summary": remediation_summary or "",
        }
    if verdict == "supersede":
        # ADR-0048: supersede repurposed. The API-side
        # ``maybe_dispatch_supersede_on_architect_verdict`` trigger owns
        # the close-PR + create-child-task + dispatch-fresh-wf-author
        # sequence; this disposition just surfaces the architect's
        # ``rewritten_description`` so the trigger can write it onto the
        # child task row. ``workflow_id=None`` signals "API-side trigger
        # handles dispatch" — same convention as the deadlock-override
        # branch of accept-as-is below.
        return {
            "workflow_id": None,
            "task_id": task_id,
            "target_artifact": target_artifact,
            "remediation_summary": remediation_summary or "",
            "rewritten_description": rewritten_description or "",
            "intent": "supersede-rewrite-task",
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
    if verdict == "gate-broken":
        # ADR-0058: the architect declares the deterministic gate is
        # the failure (sandbox-availability, missing tooling, typo'd
        # validation script — not an author defect). ``workflow_id=None``
        # signals "API-side trigger handles dispatch" — same convention
        # as the supersede + deadlock-override branches. The API-side
        # ``maybe_dispatch_gate_broken_escalation`` trigger reads the
        # step's top-level ``payload.verdict`` + ``payload.gate_log_excerpt``
        # (not this dispatch sub-object) and emits
        # ``task.escalated_to_operator`` with ``reason='gate-broken'``.
        # No successor workflow_run is dispatched — the task is parked
        # until the operator either repairs the gate (re-runs the loop)
        # or supersedes the task.
        return {
            "workflow_id": None,
            "task_id": task_id,
            "target_artifact": target_artifact,
            "intent": "gate-broken-park",
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
    workflows. (Per ADR-0048, ``uncertain`` was removed from the verdict
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


def _find_most_recent_step_by_role(
    prior_steps: list[Any], role_substr: str,
) -> dict[str, Any] | None:
    """Search prior_steps (in order) for the first step whose role_id
    contains role_substr. Return the PriorStep.output dict if found,
    None otherwise. Assumes prior_steps are ordered most-recent-first."""
    for step in prior_steps:
        if role_substr.lower() in step.role_id.lower():
            return step.output
    return None


def _contains_accept_as_is_cue(text: str) -> bool:
    """Check if text contains any of the accept-as-is prose cues."""
    if not text:
        return False
    lower = text.lower()
    cues = ("no changes needed", "already present", "accept-as-is", "nothing to remediate")
    return any(cue in lower for cue in cues)


def _short_circuit_nothing_to_do(ctx: DispositionContext) -> dict[str, Any] | None:
    """Implement the three-clause guard for ADR-0074 nothing-to-do short-circuit.

    Returns a synthetic accept-as-is envelope when all three clauses hold:
    1. Zero commits ahead of origin/main
    2. Most recent validator step verdict was "pass"
    3. Most recent author step contains accept-as-is signal

    Returns None if any clause fails; the caller falls through to normal
    architect processing.
    """
    # Clause 1: zero commits ahead of origin/main
    if not _branch_has_no_commits_against_main(ctx.repo_dir):
        return None

    # Clause 2: most recent validator verdict is pass
    validator_output = _find_most_recent_step_by_role(
        ctx.ctx.prior_steps, "validator"
    )
    if validator_output is None:
        return None
    # The validator output should have a "decision" field per ADR-0058/ADR-0027
    validator_decision = validator_output.get("decision")
    if validator_decision != "pass":
        return None

    # Clause 3: most recent author step contains accept-as-is signal
    author_output = _find_most_recent_step_by_role(
        ctx.ctx.prior_steps, "author"
    )
    if author_output is None:
        return None
    # Check explicit verdict field first
    author_verdict = author_output.get("verdict")
    if author_verdict == "accept-as-is":
        pass  # Clause 3 satisfied
    else:
        # Check prose cues in reasoning or summary
        author_reasoning = author_output.get("reasoning", "")
        if not _contains_accept_as_is_cue(author_reasoning):
            return None

    # All three clauses hold — return synthetic envelope
    envelope: dict[str, Any] = {
        "verdict": "accept-as-is",
        "reasoning": (
            "No new commits ahead of origin/main; prior author and validator "
            "steps confirm completion."
        ),
        "target_artifact": "",
        "parsed_from_prose": False,
        "short_circuit_reason": "nothing-to-do",
    }
    logger.info(
        "short-circuit: nothing-to-do detected; all three clauses hold "
        "(zero commits, validator pass, author accept-as-is)"
    )
    return envelope


def handle(ctx: DispositionContext) -> StepOutput:
    """Parse the architect verdict envelope and emit the routing
    payload. No git or PR side effects — those happen downstream when
    the coordination consumer reads ``payload.dispatch`` and fires the
    next workflow.

    Per ADR-0074, short-circuits on nothing-to-do (zero commits +
    validator pass + author accept-as-is) without making a Claude call.

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
    # ADR-0074 short-circuit: deterministic nothing-to-do check
    short_circuit_envelope = _short_circuit_nothing_to_do(ctx)
    if short_circuit_envelope is not None:
        # Return synthetic envelope without consulting Claude
        dispatch_payload = _build_dispatch_payload(
            verdict="accept-as-is",
            target_artifact=short_circuit_envelope["target_artifact"],
            remediation_summary=None,
            rewritten_description=None,
            task_id=ctx.ctx.task_id,
            trigger=ctx.ctx.trigger,
        )
        pr_comment_payload = _build_pr_comment_payload(
            verdict="accept-as-is",
            reasoning=short_circuit_envelope["reasoning"],
            target_artifact=short_circuit_envelope["target_artifact"],
        )
        payload: dict[str, Any] = {
            "verdict": "accept-as-is",
            "reasoning": short_circuit_envelope["reasoning"],
            "target_artifact": short_circuit_envelope["target_artifact"],
            "dispatch": dispatch_payload,
            "parsed_from_prose": False,
            "short_circuit_reason": "nothing-to-do",
        }
        if pr_comment_payload is not None:
            payload["pr_comment"] = pr_comment_payload
        return StepOutput(
            summary="Short-circuited: nothing-to-do (zero commits, validator pass, author accept-as-is)",
            decision="accept-as-is",
            commit_sha=None,
            artifacts=[],
            payload=payload,
            metadata=Metadata(),
        )

    summary = ctx.claude_result.summary or ""
    # ADR-0083: read the architect's verdict from the CLI's structured
    # output (forced via --json-schema in runner.py). The prose
    # fallback chain (prose cues + structured-output retry) is gone;
    # parse-failure now routes via emit-failure → cc-relay to the
    # dispatching orchestrator instead of decrementing the cap.
    envelope = _extract_verdict_envelope(
        ctx.claude_result.structured_output,
        fallback_summary=summary,
    )

    verdict: str = envelope["verdict"]

    # ADR-0083: emit-failure synthetic envelope → POST the event to
    # the API (which the trigger converts into a cc-relay drop), then
    # return StepOutput with decision='emit-failure' and no dispatch
    # payload. No downstream wf-feedback / wf-author / wf-doc-amend
    # fires; the orchestrator session sees the relay and decides
    # whether to hand-author, re-dispatch with adjusted scope, or
    # escalate to Joe.
    if verdict == "emit-failure":
        _post_architect_emit_failure(
            api_base_url=ctx.settings.api_url,
            api_timeout=10.0,
            task_id=ctx.ctx.task_id,
            failing_run_id=ctx.ctx.run_id,
            created_by=ctx.ctx.created_by or "",
            parse_failure_reason=envelope["parse_failure_reason"],
            model_output_excerpt=envelope["model_output_excerpt"],
        )
        logger.warning(
            "architect emit-failure: reason=%s; routed to cc-relay via "
            "task.architect_emit_failure event",
            envelope["parse_failure_reason"],
        )
        return StepOutput(
            summary=(
                f"architect emit-failure ({envelope['parse_failure_reason']}); "
                "routed to dispatching orchestrator via cc-relay"
            ),
            decision="emit-failure",
            commit_sha=None,
            artifacts=[],
            payload={
                "verdict": "emit-failure",
                "parse_failure_reason": envelope["parse_failure_reason"],
                "model_output_excerpt": envelope["model_output_excerpt"],
            },
            metadata=Metadata(),
        )

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
    rewritten_description: str | None = envelope.get("rewritten_description")
    gate_log_excerpt: str | None = envelope.get("gate_log_excerpt")
    # ADR-0083: supersede-missing-rewrite and gate-broken-missing-excerpt
    # subfailures were folded into _extract_verdict_envelope's post-emit
    # validation. They no longer raise here.

    dispatch_payload = _build_dispatch_payload(
        verdict=verdict,
        target_artifact=target_artifact,
        remediation_summary=remediation_summary,
        rewritten_description=rewritten_description,
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
    if rewritten_description:
        # Surface at top level too (in addition to dispatch_payload) so
        # downstream readers that inspect the verdict envelope shape
        # rather than the dispatch sub-object see the corrected task
        # text in the natural place. The API-side supersede trigger
        # reads ``dispatch.rewritten_description``.
        payload["rewritten_description"] = rewritten_description
    if gate_log_excerpt:
        # ADR-0058: surface at top level so the API-side gate-broken
        # trigger (Step 3) reads the excerpt without re-parsing the
        # disposition's prose. Per the validator-mirror above, this is
        # always populated when verdict==gate-broken.
        payload["gate_log_excerpt"] = gate_log_excerpt
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
