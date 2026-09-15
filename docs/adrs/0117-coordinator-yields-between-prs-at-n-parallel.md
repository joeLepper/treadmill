# ADR-0117: Keep each coordinator turn bounded — an interim N-parallel wedge mitigation

- **Status:** proposed — **INTERIM** operational mitigation; to be superseded by the coordinator-router ADR (move mechanical coordination into server code)
- **Date:** 2026-09-14
- **Amends (interim):** ADR-0087 (long-lived team execution model)
- **Related:** ADR-0115 (feature-branch self-drive), ADR-0116 (gate weight by blast radius), ADR-0108 (panel-backed evaluator)

## Context

ADR-0087 gives each repo ONE long-lived coordinator that routes tasks, owns all
Treadmill bookkeeping, and processes review/rework/verdict traffic in its single agent
session. The first N-parallel dogfood (`netlify/agent-runner-orchestrator`, a
4-investigation plan) OBSERVED a serialization: the coordinator processed all 4 PRs'
peer-reviews, reworks, and verdicts within ONE agent turn — a ~34k-token turn, still
growing — while idle workers waited. The DANGER inferred from that observation (not itself
observed — the run advanced, it did not wedge) is that such a turn grows toward the
context/time limit MID-orchestration and wedges or loses state on a client production repo.
The safety of a prod-repo orchestration must not depend on the coordinator happening to
self-correct to short turns (which it did this run).

This is orthogonal to gate weight. ADR-0116 cut the per-CYCLE cost (a single cross-model
pass, not the full panel), but not the review-cycle count (defect-driven) nor the per-turn
serialization. Slice-1 (heavy panel) took 3 review cycles; #1216 (light single-eval) also
reached its rework-cycle limit — same count, lighter cost — so serialization is a lever
separate from gate weight.

**New operator direction (2026-09-14), which makes this ADR interim:** the coordinator's
MECHANICAL coordination — dispatch, dependency resolution, merge, drain, teardown — should
move into a ROUTER in server code, with agents kept only for JUDGMENT (peer review,
evaluator verdicts, conflict resolution). "If this can be handled as a simple router, let's
do it" (operator). If coordination is code, there is no agent turn to grow — the
serialization point and the whole recent bug class (double-dispatch, event-wake stall, CI
rollup misclassification, all symptoms of routing encoded as fallible agent prose) dissolve
rather than get mitigated. That durable fix belongs in the coordinator-router ADR. This ADR
is the INTERIM bridge only, because the wedge risk is live on a client repo now and the
router is a larger change that will not land immediately.

## Decision

**The safety invariant: every coordinator turn stays bounded below the context/time
limit.** The coordinator must never let a single turn grow to near its limit while an
N-parallel orchestration is in flight. Two enforcement layers of that one invariant:

- **Proactive (per-turn work bound):** the coordinator dispatches a bounded batch of
  reviews/reworks per turn and integrates cleared PRs one at a time, rather than processing
  all ready PRs' traffic in one turn — it yields and picks up the remainder in a fresh turn.
- **Reactive (floor):** if a turn nevertheless nears the limit, the coordinator lands its
  in-flight integrations FIRST, then yields to a new turn — never a mid-turn wedge.

This is deliberately MINIMAL — a standing operational directive plus a short note in the
coordinator template — NOT heavy per-turn-accounting machinery, because the router
supersedes it. It is marked interim throughout.

## Alternatives considered

- **Incumbent: one coordinator, unbounded per-turn coordination (ADR-0087 as-is).** Why
  insufficient: at N-parallel it serializes N PRs' review/rework in one turn (the observed
  ~34k-token turn) with no bound, so the turn can approach the limit mid-orchestration —
  the inferred prod-repo wedge risk.
- **The durable fix — mechanical coordination as a router in code (coordinator-router
  ADR, forthcoming).** Not rejected — PREFERRED, and the reason this ADR is interim. It
  dissolves the serialization (no agent turn) and the routing-as-prose bug class. Deferred
  to its own ADR because it supersedes the agent-as-coordinator core of ADR-0087 and needs
  its own careful decision (which mechanics move, the seam, migration).
- **Multiple coordinators per repo (shard the PRs).** Rejected: ADR-0087 makes ONE
  coordinator the single owner of the repo's task-state bookkeeping; two coordinators race
  on `task_prs`, step registration, and integration order. The bottleneck is per-turn work,
  not the session count.
- **Heavy per-turn-cap machinery in the template (token/PR accounting).** Rejected as
  wasteful against the router direction — it would be throwaway. The interim mitigation is a
  directive + minimal note, not machinery.

## Consequences

### Good
- The coordinator's turn stays bounded regardless of plan parallelism — the inferred
  mid-turn wedge on a production repo is mitigated now, and in-flight integrations always
  land before a fresh turn.
- Cheap and disposable: a directive, not machinery — nothing to unwind when the router
  lands.

### Bad / trade-offs
- Yielding between PRs adds turn-boundary latency: a fully-ready batch clears over several
  turns, not one. Accepted: bounded-and-safe beats fast-and-wedge-prone on a client repo.
- It is a heuristic (a judgment about turn size), not a machine-enforced bound — precisely
  why it is interim and the router is the real fix.

### Risks
- A single EVALUATOR session is a SECOND serialization point (one evaluator × N PRs ×
  their defect-driven cycles). This ADR does not address it; that is a related, separate
  follow-up, because the evaluator produces genuine quality signal — its serialization is a
  throughput limit, not a wedge danger.
- **Falsifier:** on an N-parallel plan (N ≥ 3 ready PRs), a coordinator turn GROWS TO near
  its context/time limit while the orchestration is still in flight — instead of the
  coordinator bounding per-turn work and landing its in-flight integrations before yielding
  to a fresh turn. (The symptom is the unbounded, limit-approaching turn — not the mere act
  of touching several PRs in one small, safely-bounded turn.)

## Diagram

```mermaid
sequenceDiagram
    actor Coordinator
    participant PRqueue as N ready PRs
    participant IntegrationBranch as joes-agents/&lt;slug&gt;
    Coordinator->>PRqueue: turn 1 — dispatch a BOUNDED batch of reviews/reworks
    Coordinator->>IntegrationBranch: land any cleared integration (one at a time)
    Note over Coordinator,PRqueue: turn nears limit → land in-flight, YIELD
    Coordinator->>PRqueue: turn 2 — pick up the remainder
    Coordinator->>IntegrationBranch: land the next cleared integration
```

## References

- `docs/learnings/2026-09-14-single-coordinator-serializes-n-parallel-review-in-one-mega-turn.md`
  (the live observation this ADR crystallizes — measured ~34k-token turn, run advanced, not
  wedged).
- ADR-0087 (single coordinator per repo — interim-amended here, to be superseded by the
  coordinator-router ADR); ADR-0116 (gate weight — the orthogonal per-cycle-cost lever).
- `tools/team-templates/coordinator/CLAUDE.md.tmpl` (§9 integration loop — where the minimal
  interim directive lands).
