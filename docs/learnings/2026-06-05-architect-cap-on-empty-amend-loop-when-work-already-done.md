---
date: 2026-06-05
trigger: incident
status: captured
related: ADR-0029, ADR-0074
---

# Learning: architect cap fires on empty-amend loop when work is already done

## Trigger

Task substep-2.1 / substep-3.1 (both re-dispatched from a known-merged parent
task after operator re-queued the step): wf-author produced commits on the first
dispatch; wf-validate gate passed; wf-review approved. On re-dispatch to the
same parent task (operator requeued to confirm the loop was idempotent), a
**duplicate author run** occurred against the already-merged main branch. The
workspace had no new commits to make (the task's spec was satisfied by the prior
dispatch). The architect received an empty diff, emitted `accept-as-is`, but the
amend-cap counter (ADR-0029 Q29.e: 5 per task) incremented anyway. A second
re-dispatch burned the third cycle; a third burned the fourth; operator escalated
when the fifth `accept-as-is` reached the cap, stalling the workflow.

## Observation

The architect-amend loop (wf-author → wf-validate → wf-review → wf-architect)
has no deterministic short-circuit for the case where (1) the task branch has
zero commits ahead of origin/main (nothing new to review) AND (2) the task's
most recent validator verdict was green AND (3) the most recent author step
emitted accept-as-is. In this case, every dispatch cycle is futile: the
architect has no author defect to find and must emit accept-as-is, but the
counter still increments. After 5 such cycles, the cap fires and escalates to
the operator, even though the "failure" is not a failure — it's a structural
artifact (re-dispatch with no new work to do).

## Generalization

Pre-Claude deterministic checks can short-circuit expensive API calls when the
check's preconditions guarantee the outcome. Gate-broken detection (ADR-0058)
proves this: a deterministic check for ralph-loop deadlocks spares the architect
call entirely when the signals are clear. The same principle applies to empty-diff
cases: if the preconditions for "nothing to do" are fully determined by Git,
validator, and author state, the architect call itself is wasted budget.

## Proposed rule

When three conditions all hold — (1) the task branch has zero commits ahead of
origin/main, (2) the most recent validator step's verdict was green, and (3) the
most recent author step's envelope was accept-as-is — **the architect short-circuit
returns a synthetic `accept-as-is` verdict without invoking the architect Claude**,
and the amend-cap counter is not incremented. The short-circuit is deterministic
and fully driven by Git state + prior-step envelopes; no new Claude verdict label
is added to the system.

## Proposed remediation

Add a pre-architect short-circuit detector in `handle_architecture` (the architect
disposition's entry point) that evaluates the three conditions. On match, emit a
synthetic `accept-as-is` envelope with provenance fields (`parsed_from_prose: false`,
`short_circuit_reason: "nothing-to-do"`) and return without making the architect
Claude call. Payload shape mirrors the architect's real verdicts (ADR-0027 envelope)
so downstream routing works unchanged.

## Notes

Re-dispatch incidents: when an operator re-queues a task to confirm idempotence
(e.g. after a suspected bug is fixed), the re-dispatch against a main branch that
already has the task's commits is a common scenario. The short-circuit catches
these cases. The amend-cap (Q29.e) **remains the bound for the legitimate-amend case
where work still needs doing** — this short-circuit does not raise it; it simply
avoids burning cap budget on the case where no work remains.
