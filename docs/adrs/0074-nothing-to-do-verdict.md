# ADR-0074 — Deterministic nothing-to-do short-circuit for the architect

- **Status:** accepted
- **Date:** 2026-06-05
- **Supersedes:** none
- **Related:** ADR-0058 (gate-broken detection), ADR-0029 (architect amend cap), ADR-0031 (auto-merge), `docs/learnings/2026-06-05-architect-cap-on-empty-amend-loop-when-work-already-done.md`

## Context

The architect-amend loop can deadlock on re-dispatch when the task's precondition
(a merged parent task) has already incorporated the task's commits. A re-dispatch
of such a task produces an empty diff: the workspace has zero commits ahead of
origin/main. The architect receives empty diffs on consecutive cycles, emits
`accept-as-is` each time, but the amend-cap counter (ADR-0029 Q29.e: 5 attempts
per task) still increments. After 5 futile cycles, the cap fires and escalates
to the operator, even though the "failure" is not a failure — the task is
complete and re-dispatch simply produced no new work.

The 2026-06-05 incident surfaced this clearly: substep-2.1 and substep-3.1
(both re-dispatched from a known-merged parent) each burned 5 architect cycles
emitting `accept-as-is`, then escalated. The loop detected success (green
validation, approve review) but the empty-diff case had no short-circuit to
prevent pointless architect calls.

## Decision

Add a pre-architect deterministic short-circuit at the top of `handle_architecture`
(the architect disposition's entry point in
`workers/agent/treadmill_agent/runner_dispositions/architecture.py`).

When three conditions **all hold**, the function returns a synthetic `accept-as-is`
envelope with provenance fields and **never makes the architect Claude call**:

1. `git rev-list origin/main..HEAD --count` returns zero on the task branch —
   the workspace has no commits ahead of main.
2. The most recent validator step's verdict was green (`decision == "pass"`).
3. The most recent author step's envelope contained `verdict == "accept-as-is"`.

The synthetic envelope carries:
- `verdict: "accept-as-is"`
- `reasoning: "No new commits ahead of origin/main; prior author and validator steps confirm completion."`
- `parsed_from_prose: false`
- `short_circuit_reason: "nothing-to-do"`
- All other fields match the schema of a normal architect verdict envelope (ADR-0027)

The amend-cap counter is **not incremented** on short-circuit; the designer notes
that this path consumes neither a Claude call nor an amend budget, since there is
no author defect to iteratively remedy.

**No new Claude verdict label is added.** `_VALID_VERDICTS` in `architecture.py`
remains at four entries: `{"amend", "supersede", "accept-as-is", "gate-broken"}`.
The detection is fully deterministic; the architect prompt is not modified.

## Consequences

**Positive:**

- Empty-diff re-dispatches (re-queued task runs where the precondition is
  already merged) short-circuit immediately without architect invocation. Zero
  wasted Claude calls for the no-new-work case.
- The amend-cap (Q29.e) protects the legitimate-amend case without false
  escalations on structural non-failure (re-dispatch with success). Cleaner
  operator dashboard: re-dispatch cycles no longer burn cap budget.
- Downstream systems (DSPy bots, telemetry) gain a deterministic `short_circuit_reason`
  label to identify and train on empty-diff patterns without conflating them with
  architect-emitted verdicts.

**Negative:**

- False-positive risk: the three clauses could misclassify a real author defect
  as nothing-to-do if the prior author and validator steps are stale or
  logically unrelated to the current task spec. Mitigation: the clauses are
  conjunctive (all three must hold) and each is a strong individual signal; a
  single clause off the truth means the architect still runs. The short-circuit
  is conservative.

**Neutral:**

- The synthetic envelope shape mirrors the architect's real verdicts (ADR-0027
  pattern), so downstream dispatch and routing code sees no change; the
  `provenance` fields allow auditing where the verdict originated.

## Sequence (high level — full step list in the plan)

1. **Architect disposition entry point** — `handle_architecture` in
   `workers/agent/treadmill_agent/runner_dispositions/architecture.py`.
2. **Three-clause deterministic check** — git rev-list count, validator step
   decision, author step envelope verdict.
3. **Synthetic envelope composition** — return `StepOutput` with the short-circuit
   verdict, provenance markers, and dispatch payload matching normal architect routing.
4. **Test file** — add unit tests for the three-clause detector to verify
   short-circuit vs. architect-call paths.
5. **AGENT.md surface** — the architect component's `AGENT.md` reflects the new
   short-circuit behavior in its "Recent changes" or implementation notes.

## Alternatives considered

**Alternative A: Emit `nothing-to-do` as a fifth verdict label.**

Rejected because: the architect would still need to be called to emit the label,
which wastes the Claude call. ADR-0058's "sweep detector" reasoning applies: a
deterministic check can derive the label without invoking the LLM. The label
itself (`nothing-to-do`) is meaningful, but the architect's invocation to emit it
is the overhead we eliminate. Per ADR-0027 (envelope contract), a new verdict
would require schema migration; a synthetic envelope avoids that cost.

**Alternative B: Raise the amend-cap value.**

Rejected because: the cap bounds correctly for the legitimate-amend case where
work still needs doing. This short-circuit does not change the nature of
amendments — it simply avoids wasting cap budget on the case where no
amendments are needed. Raising the cap (e.g., from 5 to 7) would make
gate-broken and other pathological cases burn more budget before escalation;
it does not solve the empty-diff problem. The right knob is the short-circuit,
not the cap value. Per ADR-0029, the cap remains the bound for legitimate
amends.

**Alternative C: Backfill task-bound `github.pr_merged` events at re-dispatch time.**

Rejected as a *durable* fix because: the backfill is the correct **operator-side
workaround** when re-dispatching known-merged work (the operator can pre-signal
merger before re-queuing), but it does not help the general empty-diff case.
Many empty-diff scenarios arise from concurrent progress outside the task
(a parallel task whose precondition shipped through a sibling PR, not via
out-of-band merge). A backfill only covers the explicit re-dispatch case.
The short-circuit (this ADR) catches all empty-diff scenarios regardless of
their origin. Backfill is still recommended as an operator practice for
re-dispatch, but it is not sufficient alone.
