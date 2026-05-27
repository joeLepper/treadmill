# ADR-0058 — Architect `gate-broken` verdict for ralph-loop deadlocks

- **Status:** proposed
- **Date:** 2026-05-27
- **Supersedes:** none
- **Related:** ADR-0029 (architect amend cap), ADR-0015 (workflow + role reuse), `docs/learnings/2026-05-27-deterministic-gates-must-run-in-worker-sandbox.md`

## Context

When the deterministic validation gate fails for reasons outside the
author's control — `cdk synth` against an unreachable AWS account,
`docker` calls in a daemonless sandbox, a typo in the task's validation
script — the architect-amend loop becomes a deadlock. The author
produces logically-correct code, the gate stays red, the architect
verdicts `amend`, wf-feedback re-authors, the gate stays red, repeat.
The ADR-0029 cap (≥5) bounds the runaway but only after wasted cycles.

The 2026-05-27 RAMJAC incident surfaced this clearly. Five tasks
hit the cap on identical `cdk synth + make unit_test` gates that the
worker sandbox could not satisfy. The architect's amend rationale on
the final attempt even named the pattern:

> The task is **Trigger B (ralph-loop deadlock)**.

The architect *knew* it was a deadlock. It had no verdict to act on
that knowledge. The available verdicts (`amend` / `supersede` /
`accept-as-is`) all assume the author is the failing party. Result:
every gate-broken task burns its full cap before reaching the
operator.

## Decision

Add a fourth architect verdict: **`gate-broken`**.

Emitted when the architect's classifier identifies a ralph-loop
deadlock — typically signaled by ≥2 consecutive author cycles where:

- The author's prose / commits indicate the deliverables are present.
- The deterministic gate is the sole blocking failure.
- The gate's stderr suggests sandbox-availability, not a code defect
  (e.g. exit codes from `cdk synth`, `aws`, `docker`, "command not
  found", "Unable to locate credentials").

The verdict's effect:

1. **Escalate to operator on first detection** — same path as the
   existing `task.escalated_to_operator` event, but with a distinct
   reason code (`gate-broken`) so dashboards / sweeps can triage
   separately from architect-cap escalations.
2. **Do NOT increment the amend-cap counter** — this isn't an author
   problem; the cap protects against a different failure mode.
3. **Carry the gate's full stderr and the architect's classification
   reasoning** in the payload, so the operator can repair the gate
   without re-running the loop to reproduce the failure.
4. **Park the run as deferred** — the workflow_run stays in a
   `gate-broken` derived state until the operator either rewrites the
   gate (triggering a new author cycle) or supersedes the task.

The architect's prompt gains a "Trigger B detection" section that
walks the classifier and chooses `gate-broken` over `amend` when the
signals are present. We do not remove `amend` — author defects remain
the common case and `amend` is the right verdict there.

## Consequences

**Positive:**

- Gate-broken tasks surface to the operator on **detection #2** (one
  cycle plus one classifier confirmation) instead of after 5 amend
  cycles. ~75% reduction in wasted dispatch budget per wedged task,
  applied across the RAMJAC fleet.
- Operator dashboards gain a distinct bucket for gate failures vs.
  legitimate cap escalations, making the "architect too permissive"
  signal (project memory) cleaner — gate-broken cases no longer pollute
  the architect-cap denominator.
- The plan skill's worker-sandbox rule (added 2026-05-27) gets a
  matching enforcement mechanism. A plan that violates the rule
  surfaces fast, with clear diagnostics, instead of looping silently.

**Negative:**

- Adds a fourth verdict to the architect prompt; non-trivial prompt
  change risks regressions in the existing three verdicts' precision.
  Mitigation: ADR-0053's prompt-tuning harness can A/B the new prompt
  against the old on the labeled corpus before cutover.
- A false-positive `gate-broken` verdict (architect mistakes a real
  author bug for a gate problem) wastes an operator cycle. Mitigation:
  the classifier requires ≥2 consecutive author cycles + structural
  cues from the stderr; the operator path is still bounded.
- The architect role gains state (it must inspect the run history to
  count consecutive author cycles). Mitigation: small, the data lives
  in events; the role already inspects the prior step's output to
  reason about amend feedback.

**Neutral:**

- `workflow_runs.task_id` continues to be nullable for historical
  reasons (ADR-0057); the new derived state composes cleanly with the
  existing state machine.

## Sequence (high-level — full step list in the plan)

1. Schema: add `gate-broken` to `ArchitectVerdict.verdict` Literal +
   the worker-side `_VALID_VERDICTS` set + parser cues.
2. Architect prompt: add Trigger B classifier + verdict-selection rule.
3. Event: add `task.gate_broken` event (or extend
   `task.escalated_to_operator` with a reason field) so dashboards can
   filter.
4. Dispatch: gate-broken verdict skips the amend-cap counter; parks
   the workflow_run in a derived `gate-broken` state.
5. Stderr-capture hotfix (sibling, see plan): the architect needs full
   gate stderr to classify confidently — `validation_runtime.py` line
   42 caps `log_excerpt` at ~2000 chars but the architect prompt may
   re-truncate further; audit + extend.
6. Operator dashboard: render the gate-broken bucket distinctly.

## Alternatives considered

**Detect deadlocks via a deterministic sweep** (like
`wf-stuck-task-sweep`) and escalate from there. Rejected because: the
sweep is reactive (runs every 10m) and would still let the cap fire
first in most cases; the architect already has the signal in-loop, so
giving it a verdict is cheaper than building a sibling detector.

**Let the architect rewrite the validation script** as part of the
`amend` verdict. Rejected because: the architect's mandate is code
correctness, not gate authorship; conflating the two erodes the role
boundary. The operator should own gate repairs (or the plan skill
should re-emit the plan).

**Raise the amend cap** so the loop survives longer on legitimate
amends. Rejected because: the cap already bounds runaway *correctly*;
the problem is the wedge type, not the cap value. Raising the cap
makes gate-broken tasks waste more budget, not less.
