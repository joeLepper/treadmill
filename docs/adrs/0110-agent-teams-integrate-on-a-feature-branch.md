# ADR-0110: Agent teams integrate on a feature branch, not main

- **Status:** accepted (2026-09-13; cross-model panel review — 5/6 blocking, folded — + sibling co-sign by Ernie, verified in-text); amended by ADR-0114 (per-plan `integration_base`) + ADR-0115 (feature-branch self-drives: no CI gate, inline depends_on)
- **Date:** 2026-09-13
- **Related:** ADR-0087 (team execution / coordinator merges), ADR-0109 (team lifecycle), ADR-0108 (panel-backed evaluator)

## Context

Almost every repo we work in makes agent-merge-to-main impossible: branch protection,
human-merge-only org policy, or the agent identity simply lacking merge permission
(zephyr's rule is "agents open PRs, never merge to main"; `joelepper-zephyr does not have
permission` blocked a `gh pr merge` this very session). ADR-0087 has the coordinator
merge task PRs, but when the base is `main` the coordinator CANNOT merge on these
repos — so the team stalls, or every PR needs a human merge and the team never runs
unfettered. exec-in-charge already anticipates this ("Base ≠ always `main`; some plans
target an integration or planning branch"), but it is an exception, not the model.

## Decision

An agent team integrates its work on a per-plan **feature branch**, never directly on
`main` by default.

- At standup / plan-submit the team creates an integration branch
  **`joes-agents/<branch-slug>`** off `main` (`<branch-slug>` derived from the plan —
  unique per plan).
- Worker task PRs base onto the integration branch; the evaluator + cross-model panel
  review against it; CI runs on it; the coordinator INTEGRATES an approved task by
  **git merge + push** to the integration branch — NOT `gh pr merge`. This matters:
  the block on most repos is the agent identity lacking the PR-MERGE API permission
  (the cited `joelepper-zephyr does not have permission` case) or `main` protection; a
  plain `git push` to an unprotected `joes-agents/*` branch, which the agent already
  has write access for, sidesteps both. depends_on ordering resolves within the branch.
- **Standup PREFLIGHT (the premise must be VERIFIED, not assumed).** At standup the
  manager verifies the agent can actually integrate on `joes-agents/<slug>` — push
  access AND no protection/ruleset blocking it (a test push of an empty commit, or a
  rules check). If it CANNOT (identity has no write at all, or a wildcard/all-branch
  ruleset covers `joes-agents/*`), feature-branch mode does NOT help — the standup
  FAILS LOUDLY and escalates to the operator; it never silently stalls the team. Honest
  scope: this mode solves the `main`-specific-protection and PR-merge-API-permission
  cases; a repo that blocks ALL agent writes genuinely needs a human in the loop and
  should not run an autonomous team.
- The plan is **"implemented"** when all its tasks are integrated into the branch AND
  the `branch → main` handoff PR is OPENED and RECORDED (below). Teardown is gated on
  that recorded handoff — never on "last task merged" alone — so implemented work is
  never orphaned by a team that dissolved before the gate exists.
- The **human owns the final gate**, but the TEAM's last act is to OPEN the
  `joes-agents/<branch-slug>` → `main` PR, record its URL on the plan, AND SURFACE the
  handoff to the operator — a distinct event/escalation ("plan X implemented on
  `joes-agents/<slug>`; ready for your `branch → main` PR"). Without this notification
  an ephemeral team would implement, tear down, and leave the branch sitting with no
  team and no signal — silently orphaned, and the orphan falsifier below would be
  UNOBSERVABLE. The operator then resolves the PR against `main` through the repo's own
  **full `main` CI** + human review — a semantic break (e.g. `main` changed an API the
  branch uses, with no textual conflict) surfaces ONLY at that full CI, not on the
  green integration branch. Teardown (ADR-0109) fires only after the handoff PR is
  opened, recorded, AND surfaced; if it later fails CI or conflicts, the operator owns
  it (the team's job ended at a clean, reviewed integration branch + a surfaced handoff
  PR). Team velocity is decoupled from the `main` gate, but the handoff always exists
  and is always announced before the team dissolves.
- **Merge target is a per-repo mode**: `feature-branch` (default) | `main` (only the
  rare repo that actually permits agent-merge-to-main). This is the second repo-mode
  axis alongside ADR-0109's lifecycle mode.

## Alternatives considered

- **Incumbent: the coordinator merges task PRs to `main` (ADR-0087 default).** Why
  insufficient: blocked on almost every repo (protection / perms), so the team stalls
  or falls back to per-PR human merges — it cannot run unfettered.
- **Per-PR human merge to `main`.** Rejected: the human is a bottleneck on every PR;
  the whole point is to let the team integrate freely and gate ONCE at `main`.
- **A long-lived shared integration branch across plans.** Rejected: entangles
  unrelated plans, and it breaks the clean "implemented = all this plan's tasks merged"
  teardown trigger. Per-plan branch is isolated and maps to the lifecycle.

## Consequences

### Good
- Teams run unfettered on gated repos (the common case) — no per-PR human merge.
- The `main` gate is preserved: one human-reviewed PR through the repo's own CI.
- Defines the "implemented" teardown trigger ADR-0109 needs, and keeps the team's work
  entirely off `main` — so the post-merge deploy/rollback concern lives on the operator's
  `branch → main` PR, not in the team's drain-guard.

### Bad / trade-offs
- The integration branch drifts from `main`. Mitigation is DEFINED, not hand-waved: the
  **coordinator** merges `main` into the integration branch on a cadence (on each
  observed `main` advance, else daily); a textual conflict it can resolve trivially it
  resolves and pushes, otherwise it spawns a rebase/conflict-resolution TASK (reviewed
  like any task) or escalates — an undefined "periodically" would never happen and drift
  would grow unbounded.
- A green integration branch is NOT proof of green against `main` — the final PR re-runs
  the full `main` CI, and a SEMANTIC break (no textual conflict) surfaces only there. So
  "implemented" ≠ "shipped"; the human gate is real.
- **Cross-plan `depends_on` now serializes through the human `main` gate.** Intra-plan
  deps resolve on the branch, but a plan branched off `main` cannot see ANOTHER plan's
  work until that plan's `branch → main` PR is human-merged. For a repo running several
  dependent plans, that is a real sequencing constraint (accepted: correctness + the
  human gate over cross-plan throughput).
- **A persistent team (ADR-0109) drives one branch PER PLAN.** With two concurrent
  plans it drives TWO integration branches and MUST route each task PR to its own
  plan's branch — the coordinator keys the integration target on the task's plan, never
  a shared branch (which would be the rejected long-lived-shared-branch failure).

### Risks
- **Falsifier:** on a `feature-branch`-mode repo, a team's task PR is based on or merged
  to `main` directly (bypassing the integration branch); OR a team is stood up and
  begins work when the standup preflight could not confirm the agent can integrate on
  `joes-agents/<slug>` (it then stalls exactly like the incumbent); OR a plan reaches
  "implemented" and the team tears down WITHOUT an opened, recorded, and SURFACED
  `branch → main` handoff PR (work silently orphaned); OR two plans share one
  integration branch.

## Diagram

```mermaid
sequenceDiagram
    actor Worker
    participant Coordinator
    participant IntegrationBranch as joes-agents/&lt;slug&gt;
    actor Human
    participant Main
    Coordinator->>IntegrationBranch: create off main (standup preflight: can integrate?)
    Worker->>Coordinator: task PR (base = integration branch)
    Coordinator->>IntegrationBranch: git merge + push (evaluator+panel approved)
    Note over Coordinator,IntegrationBranch: all tasks integrated
    Coordinator->>Main: open branch → main PR (team's LAST act)
    Coordinator-->>Human: SURFACE handoff (plan implemented on joes-agents/&lt;slug&gt;, PR #N)
    Note over Coordinator,Human: "implemented" = integrated + PR opened + recorded + surfaced → teardown eligible
    Human->>Main: resolve + merge via FULL main CI + review
```

## References

- ADR-0087 (coordinator merge model), ADR-0109 (team lifecycle + the "implemented" trigger).
- exec-in-charge skill ("Base ≠ always `main`"); the zephyr "agents never merge to main" rule.
