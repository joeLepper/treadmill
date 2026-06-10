---
date: 2026-06-10
trigger: correction
status: captured
related: ADR-0084, ADR-0086
---

# Learning: All repos are coordinator-owned; no two-tier dispatch model

## Trigger
While auditing the Treadmill architecture gap (2026-06-10 session), Bert and I recommended a
"surgical" Phase 1 fix: skip `dispatch_task` on plan submit only for repos whose
`team_configs.coordinator_label` is set, leaving non-coordinator repos on the old autoscaler
path. Joe corrected this: "There isn't a distinction to be made between coordinator-owned repos
and anything else. There are only going to be coordinator-owned repos."

## Observation
We designed the migration as a two-tier system — coordinator-owned repos get the new path,
everything else keeps the old autoscaler path — because we were trying to avoid breaking
existing consumers. But there are no consumers left: the autoscaler is off, the Docker worker
model is retired, and every repo that Treadmill manages will have a coordinator + worker team.
The two tiers were an artifact of incremental thinking, not a system requirement.

## Generalization
When we are replacing an entire execution model, we tend to design migrations that preserve
the old path "for safety." That instinct is wrong when the old path has no remaining consumers.
Preserving a dead code path adds complexity without providing safety. The right move is a clean
cut with a clear before/after.

## Proposed rule
When a system component is being fully replaced and has no remaining consumers, delete it
globally rather than gating the delete behind a feature flag or repo-level condition.

## Proposed remediation
Before proposing a "surgical" migration that preserves the old path for some subset of
consumers, audit whether any consumers actually remain. If the answer is zero, propose a
clean delete instead.

## Notes
Related: the same session established the final role model — orchestrators (alan/bert/carla/donna)
are executives only; workers are separate long-lived named sessions (worker-adam, worker-bethany,
etc.); coordinators are one per repo. When a new repo is registered, a full software team
(coordinator + workers) is stood up.
