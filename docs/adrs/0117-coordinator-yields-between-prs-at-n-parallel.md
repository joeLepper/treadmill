# ADR-0117: The coordinator yields between PRs at N-parallel — a per-turn work cap

- **Status:** proposed
- **Date:** 2026-09-14
- **Amends:** ADR-0087 (long-lived team execution model)
- **Related:** ADR-0115 (feature-branch self-drive), ADR-0116 (gate weight by blast radius), ADR-0108 (panel-backed evaluator)

## Context

ADR-0087 gives each repo ONE long-lived coordinator that routes tasks, owns all
Treadmill bookkeeping, and processes review/rework/verdict traffic for the repo's plans.
The first N-parallel dogfood (`netlify/agent-runner-orchestrator`, a 4-investigation plan)
exposed a scaling limit: the single coordinator processed all 4 PRs' peer-reviews,
reworks, and evaluator verdicts SERIALLY within ONE agent turn — a ~40k-token / ~10-minute
mega-turn — while idle workers waited. Two consequences: idle workers wasted the
parallelism, and — the dangerous one — the coordinator's turn grew toward its context/time
limit MID-orchestration, risking a wedge or lost state on a client production repo.

This is orthogonal to gate weight. ADR-0116 cut the per-CYCLE cost (a single cross-model
pass instead of the full panel), but NOT the cycle COUNT (defect-driven) nor the
per-turn SERIALIZATION. Slice-1 (heavy panel) took 3 cycles; #1216 (light single-eval)
also reached the cap — same count, lighter cost. So the dominant grind at N-parallel is
the coordinator serialization, a separate lever from gate weight. The coordinator
self-corrected to short turns during the dogfood (it is not always wedged), but the safety
of a prod-repo orchestration must not depend on that luck.

## Decision

At N-parallel, the coordinator MUST NOT process all ready PRs' review/rework dispatch in a
single agent turn. It caps per-turn work and YIELDS: it lands the integrations already
cleared, dispatches a bounded number of reviews/reworks, and picks up the remainder in a
fresh turn — so its turn never approaches the context/time limit mid-orchestration. The
binding operational rule: if a turn nears a limit while orchestration is in flight, the
coordinator lands its in-flight integrations FIRST, then resumes the rest in a new turn,
rather than risk a mid-turn wedge. Integrations are processed one at a time as each PR
clears its gate, never batched into one mega-turn.

## Alternatives considered

- **Incumbent: one coordinator, unbounded per-turn coordination (ADR-0087 as-is).** Why
  insufficient: at N-parallel it serializes N PRs' review/rework in one turn that grows
  toward the context limit — a mid-orchestration wedge risk on a production repo, observed
  live (the ~40k-token turn on the netlify dogfood).
- **Multiple coordinators per repo (shard the PRs).** Rejected: ADR-0087 makes ONE
  coordinator the single owner of the repo's task-state bookkeeping; two coordinators race
  on `task_prs`, step registration, and integration order — the exact shared-state hazard
  the single-owner model prevents. The bottleneck is per-TURN work, not the session.
- **A max-turn-size guard only (land in-flight, then stop).** Kept — but as the SAFETY
  FLOOR inside this decision, not the whole fix. A guard alone still lets the coordinator
  attempt all N in one turn and only bail near the limit; the per-turn cap + yield keeps
  turns bounded by design, and the guard catches the case where a bounded turn still runs
  long.
- **Lighten the gate further (fewer cycles).** Rejected as the fix here: cycle count is
  defect-driven, and ADR-0116 already lightened per-cycle cost. Reducing gate rigor to cut
  serialization would trade correctness for throughput — the wrong lever.

## Consequences

### Good
- The coordinator's turn stays bounded regardless of plan parallelism — no mid-turn wedge
  on a production repo, and in-flight integrations always land before a fresh turn.
- Idle workers see their reviews/reworks dispatched across turns instead of stalling behind
  one serial mega-turn.

### Bad / trade-offs
- Yielding between PRs adds turn-boundary latency: a plan's N PRs clear over several turns
  rather than one, so wall-clock for a fully-ready batch may rise slightly. Accepted:
  bounded-and-safe beats fast-and-wedge-prone on a client repo.
- The coordinator template must carry an explicit per-turn cap + yield discipline, which is
  a heuristic (a token/PR-count threshold), not a hard machine limit.

### Risks
- A single EVALUATOR session is a SECOND serialization point (one evaluator × N PRs ×
  their defect-driven cycles). This ADR addresses only the coordinator; evaluator fan-out
  is a related, separate decision (a follow-up ADR), because the evaluator produces genuine
  quality signal and its serialization is a throughput limit, not a wedge danger.
- **Falsifier:** on an N-parallel plan (N ≥ 3 ready PRs), the coordinator processes all of
  them — the full set of peer-reviews, reworks, and verdicts — within a SINGLE agent turn
  whose size approaches the context/time limit, instead of capping per-turn work and
  landing in-flight integrations before yielding to a fresh turn.

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
  (the live observation this ADR crystallizes).
- ADR-0087 (single coordinator per repo); ADR-0116 (gate weight — the orthogonal
  per-cycle-cost lever); ADR-0115 (feature-branch self-drive).
- `tools/team-templates/coordinator/CLAUDE.md.tmpl` (§9 integration loop — where the
  per-turn cap + yield discipline lands).
