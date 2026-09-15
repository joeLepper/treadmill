---
date: 2026-09-14
trigger: pattern
status: crystallized-into-ADR-0117
related: ADR-0087, ADR-0110, ADR-0115, ADR-0116
---

# Learning: the single coordinator serializes N-parallel review/rework in one mega-turn

## Trigger
On the netlify dogfood's part-2 plan (4 parallel investigations under the ADR-0116 light
gate), Donna + Carla observed: the SINGLE coordinator juggles all 4 PRs' peer-reviews +
reworks + evaluator verdicts in ONE mega-turn (~10 min / 34k tokens and growing), while
workers 2 & 3 sit idle "waiting for coordinator response." ~30 min in, 0 of 4 integrated.

## Observation
Throughput is COORDINATOR-bound, not worker-bound: the coordinator processes the parallel
plan's review/rework/verdict traffic SERIALLY within a single agent turn, so N-parallel
worker output funnels through one serial per-turn processor. Two consequences: (1) idle
workers = wasted parallelism; (2) — the dangerous one — the coordinator's mega-turn grows
toward a context/time limit MID-orchestration, risking a wedge or lost state on a
production repo. The single-cross-model gate (ADR-0116) is working (catching real reworks)
and the run is advancing (not wedged) — the bottleneck is the coordinator's serial
per-turn dispatch, orthogonal to gate weight.

## Generalization
A single coordinator per repo (ADR-0087) is a serialization point that does not scale
with plan parallelism: as N-parallel tasks rise, the coordinator's per-turn work grows
and can approach the agent's turn/context limit. The gate-weight tuning (ADR-0115/0116)
reduces per-task cost but does not remove the single-turn serialization.

## Proposed rule
At N-parallel, the coordinator must NOT block on all PRs' review/rework dispatch in one
turn. Options (machinery, an ADR-shaped follow-up): batch fewer PRs per turn, YIELD
between PRs (let in-flight work land, pick up the rest in a fresh turn), or a max-turn-size
guard that lands in-flight work and defers the rest before the turn nears a limit.

## Proposed remediation
Deferred (not urgent — the current run advances, and the operational threshold is: if the
coordinator's turn nears a limit, let it land in-flight work + resume in a fresh turn,
rather than risk a mid-turn wedge). The durable fix is a coordinator-machinery ADR: a
per-turn work cap / yield-between-PRs discipline for parallel plans. Pairs with the
gate-weight ADRs (0115/0116) as the parallelism-scaling half of the same tuning.

## Notes
Caught live on the netlify dogfood 2026-09-14 (part-2, 4 parallel investigations). The
danger is specifically a mid-orchestration wedge on a client production repo, so the
operational mitigation (land in-flight + fresh turn) matters even before the machinery
fix lands.
