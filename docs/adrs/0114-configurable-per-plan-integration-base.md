# ADR-0114: A plan can set the integration base its feature branch is cut from

- **Status:** proposed
- **Date:** 2026-09-14
- **Amends:** ADR-0110 (agent teams integrate on a feature branch, not main)
- **Related:** ADR-0109 (team lifecycle), ADR-0113 (PR-state polling for webhookless repos), ADR-0031 (plan-doc frontmatter / auto_merge precedent)

## Context

ADR-0110 gave every feature-branch plan a per-plan integration branch,
`joes-agents/<branch-slug>`, and the coordinator (template §3.1a) cuts it off
`origin/main` and drifts it against `origin/main`, then at plan completion (§9.7) opens
a handoff PR `branch → main`.

Two real cases break the hardcoded `main` assumption, both surfaced by the first
collaborator-repo dogfood (ADR-0113, `netlify/agent-runner-orchestrator`):

- **The tasks must read docs/code that live only on a non-`main` branch.** Donna's
  run-shape plan's tasks read ADRs + an impl plan that exist only on
  `joes-agents/run-shape-telemetry-design`. Cut from `main`, the workers never see them.
- **We must never touch the repo's `main`.** On a client's production repo we have push
  access but their `main` is theirs. The §9.7 `branch → main` handoff PR would target
  their `main` — a boundary violation. The operator's directive (2026-09-14, via the
  netlify-relationship owner) was explicit: "not merging into main but into integration
  branches"; the integration branch itself is the deliverable the repo owner reviews.

## Decision

We added a per-plan **`integration_base`** — a git branch name, default `main` — set in
the plan doc's YAML frontmatter (`integration_base: joes-agents/<design-branch>`),
persisted on the plan record (nullable column; NULL = `main`), and exposed on
`GET /api/v1/plans/{id}`. It changes three coordinator behaviors, and ONLY these:

1. **Base (§3.1a).** The integration branch is cut from `origin/<integration_base>`, not
   `origin/main`.
2. **Drift (§3.1a Drift).** The coordinator drifts the integration branch against the
   SAME `origin/<integration_base>`, never `main`.
3. **Handoff (§9.7).** When `integration_base` is `main` (default), behavior is
   UNCHANGED: open the `branch → main` handoff PR, record + surface, park-on-human. When
   `integration_base` is NON-`main`, the coordinator opens NO PR — the integration branch
   IS the deliverable — and records the same `plan.handoff_pr_opened` signal WITHOUT a
   `pr_number` (the drain-guard keys teardown on the event's existence, not the number).

The integration branch NAME is unchanged — still `joes-agents/<branch-slug>` derived
from the plan doc basename (the date-prefixed disambiguator, ADR-0110). `integration_base`
sets only the BASE, not the name.

## Alternatives considered

- **Incumbent: hardcoded `origin/main` (ADR-0110).** Why insufficient: it cannot express
  a plan whose tasks depend on a non-`main` branch, and it forces a `branch → main`
  handoff PR onto repos whose `main` we must not touch. Both are real, not hypothetical.
- **Pre-create the integration branch off the design branch, leave the template alone.**
  Rejected: the coordinator's §3.1a "create if not exists" would reuse it, but §Drift
  still merges the repo's fast-moving `main` in, and §9.7 still opens a `branch → main`
  PR — the two boundary violations remain. A stopgap, not a fix.
- **A `treadmill team up` / `plan submit` flag instead of frontmatter.** Rejected: the
  base is a property of the PLAN (which docs its tasks read), not of the repo's team or
  the submit invocation; frontmatter co-locates it with the plan and follows the
  `auto_merge` precedent (ADR-0031), which the coordinator already reads at §9.3.
- **Point the handoff PR at the design branch instead of suppressing it.** Rejected: the
  design branch is a scaffold, not a merge target; the operator's model is that the
  integration branch itself is the reviewed deliverable, so any PR is wrong.

## Consequences

### Good
- A plan whose tasks depend on a non-`main` branch runs correctly (workers see the docs).
- A collaborator/production repo whose `main` we must not touch is fully supported: no
  branch created off their `main`, no drift from their `main`, no PR to their `main`.
- Default behavior is byte-for-byte ADR-0110 (NULL column → `main`), so no existing plan
  changes.

### Bad / trade-offs
- One more per-plan knob. Mitigated: it defaults to the current behavior and follows the
  established `auto_merge` frontmatter → column → PlanResponse path exactly.
- `PlanHandoffPrOpened.pr_number` becomes optional. Verified safe: only the scheduler's
  drain SQL reads it (`(payload->>'pr_number')::int`), which yields no match on NULL —
  correct for a handoff with no PR.

### Risks
- A `integration_base` naming a ref that does not resolve at standup would strand the
  plan. Mitigated: §3.1a step 1 fails the write-preflight loudly (`standup_preflight_failed`)
  when `origin/<base>` does not resolve after fetch.
- **Falsifier:** a plan with `integration_base: <non-main>` whose coordinator still cuts
  the branch off `origin/main`, drifts from `main`, or opens a `branch → main` handoff PR
  — any of the three means the base is not honored.

## References

- `services/api/treadmill_api/parsers/plan_doc.py` (`PlanFrontmatter.integration_base`),
  `models/plan.py` + migration `20260914_0100`, `routers/plans.py` (store + expose),
  `events/plan.py` (`PlanHandoffPrOpened.pr_number` optional),
  `tools/team-templates/coordinator/CLAUDE.md.tmpl` §3.1a / Drift / §9.7.
