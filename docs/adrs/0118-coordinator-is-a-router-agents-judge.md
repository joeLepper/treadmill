# ADR-0118: The coordinator is a router (code); the agents that judge are the workers and the validator

- **Status:** proposed
- **Date:** 2026-09-14
- **Amends:** ADR-0087 (long-lived team execution model) — supersedes its *coordinator-as-agent* core; the worker + validator + peer-review model it defines is kept.
- **Supersedes:** ADR-0117 (coordinator yields between PRs) — that per-turn cap is an interim bridge; this removes the agent turn it was bounding.
- **Related:** ADR-0108 (panel-backed validator), ADR-0109 (team lifecycle), ADR-0110 (feature-branch integration), ADR-0113/0114/0115/0116 (the collaborator-repo dogfood series), ADR-0111 (two-pass review).

## Context

ADR-0087 gives each repo one long-lived **coordinator agent**. It routes tasks, owns all
Treadmill task-state bookkeeping, sequences peer-review and the validator, resolves
`depends_on`, and integrates approved PRs into the per-plan branch by its own `git merge +
push`. It is an LLM agent: its behavior is a large prose section of
`tools/team-templates/coordinator/CLAUDE.md.tmpl` that a Claude session executes turn by
turn.

The first collaborator-repo dogfood (`netlify/agent-runner-orchestrator`, the ADR-0113
series) exposed two facts about that choice:

1. **The coordinator is a serialization point that cannot scale and can wedge.** On an
   N-parallel plan the single coordinator processed all PRs' review/rework/verdict traffic
   serially inside one agent turn (~40k tokens, ~10 minutes), growing toward the
   context/time limit mid-orchestration — a wedge risk on a client production repo
   (ADR-0117). Workers sat idle behind the serial turn.
2. **A recurring class of bug lived in the coordinator's prose.** Every routing defect this
   dogfood surfaced — the double-dispatch of a dependent (ADR-0115), the
   observer→coordinator event that never woke the coordinator, the multi-suite / `skipped`
   CI mis-rollup (ADR-0116) — was a mechanical state-machine failure encoded as fallible
   English in a template. Each was caught in review by verifying the mechanism against the
   claim; a deterministic implementation could not have had them.

The root observation: the coordinator's job is almost entirely **mechanical** — dispatch a
task when its dependencies are satisfied, register its PR, run the integration merge, apply
the drain-guard, resolve `depends_on`, stand up and tear down. An agent turn is the wrong
substrate for that. It has a finite context (hence the serialization and the wedge), and it
runs a state machine as prose (hence a bug class deterministic code cannot have). Much of
this logic already exists server-side — the scheduler, the drain-guard endpoint, the
mergeability views — and the coordinator agent partly *re-drives it through prose*.

The **judgment** in the team model does not live in the coordinator. It lives in the
**workers** (how to implement a task; whether a sibling's PR is sound in peer review; how to
resolve a merge conflict) and in the **validator** (the review verdict, panel-backed per
ADR-0108/0116). The coordinator makes no irreducible judgment call: everything it "decides"
is either mechanical routing or is delegable to a worker as a task.

## Decision

We decided the **coordinator is a router implemented in server code, not an agent.** All
mechanical coordination moves into the Treadmill router; agent turns are reserved for
judgment, and the only judgment agents are the **workers** and the **validator**. The
router invokes those agents and reads their structured results; it holds no agent turn of
its own.

- **The router (server code) owns:** task dispatch on dependency-satisfaction; `depends_on`
  resolution; `task_prs` and task-state bookkeeping; the integration `git merge + push` to
  the per-plan branch (ADR-0110/0114 base rules preserved); the drain-guard and
  teardown/standup lifecycle (ADR-0109); sequencing across a plan's tasks; and the
  gate-position choice (ADR-0116 breadth by `merge_target`). These are deterministic,
  idempotent, DB-backed handlers.
- **The workers (agents) own:** implementing a task; peer-reviewing a sibling's PR (the
  ADR-0087 §8 inner loop, kept); and resolving a merge conflict when the router detects one
  (dispatched as an ordinary task, not resolved by the router).
- **The validator (agent) owns:** the review verdict — the panel-backed evaluation and its
  rework signal (ADR-0108/0116), invoked by the router per PR.

The seam is narrow: the router **calls** workers and the validator through the existing
job/workflow dispatch and **reads** their PRs and structured verdicts. Because it never
reasons, it has no context window to grow and no turn to serialize; because its handlers are
DB-backed with the usual unique constraints, it cannot double-dispatch or lose an event.

This **eliminates** rather than mitigates the two dogfood problems: there is no agent turn,
so no serialization point and no mid-orchestration wedge (throughput is bounded only by
worker/validator capacity, which the router can fan out); and the routing bug class becomes
deterministic, testable code with database invariants. ADR-0117's per-turn cap is therefore
an interim bridge, superseded here.

## Alternatives considered

- **Incumbent: the coordinator agent (ADR-0087).** Why insufficient: it serializes
  N-parallel work in one growing turn (a prod-repo wedge risk, observed live) and encodes a
  mechanical state machine as agent-prose (the recurring double-dispatch / event-wake /
  CI-rollup bug class). The judgment that would justify an agent lives in the workers and
  the validator, not the coordinator.
- **Bound the coordinator's turn (ADR-0117: per-turn cap + yield).** Rejected as the durable
  fix: it treats the symptom (turn growth) but keeps the agent substrate, so the bug class
  and the serialization root remain. Kept only as the interim operational bridge until the
  router lands.
- **Shard into multiple coordinator agents per repo.** Rejected: ADR-0087 made one
  coordinator the single owner of the repo's task-state to prevent two writers racing
  `task_prs` and integration order. A router is single-writer by construction (serialized DB
  writes) and carries none of an agent's per-turn limits — it scales without the race.
- **Keep some judgment in the coordinator (brief/context assembly, conflict triage).**
  Rejected per the operator's scoping (2026-09-14): brief and context assembly is a
  service/tool (the context service already compiles per-task briefs), and conflict
  resolution is a dispatched worker task. The coordinator retains no irreducible judgment, so
  it need not be an agent at all.

## Consequences

### Good
- No agent turn to grow: no serialization point and no mid-orchestration wedge on a
  production repo; N-parallel throughput is bounded by worker/validator capacity, which the
  router fans out.
- The routing bug class (double-dispatch, missed event-wake, CI mis-rollup) becomes
  deterministic code with DB invariants and tests — the class we repeatedly caught in review
  is removed by construction, not guarded against by prose.
- Consolidates coordination logic that already partly lives server-side (scheduler,
  drain-guard, mergeability views), ending the coordinator agent's prose duplication of it.
- Agents do only what needs judgment (implement, peer-review, validate) — cheaper, clearer,
  and each is independently parallelizable.

### Bad / trade-offs
- This is a real build, not a prose edit: new/extended router endpoints, migrations, a
  dispatch loop, and a migration off the coordinator template. Larger blast radius than the
  dogfood ADRs — it needs a sequenced plan and a staged rollout.
- Behaviors that currently read as coordinator "judgment" (when to escalate, conflict
  triage) must each be made explicit as either a deterministic rule in the router or an
  explicit dispatched-agent decision. Anything found mid-build that genuinely needs judgment
  must route to a worker or the validator — never sneak back into the router as prose.
- The feature-branch self-drive (ADR-0115), the per-plan `integration_base` (ADR-0114), and
  the gate-weight split (ADR-0116) all currently run through the coordinator; the router must
  preserve every one of those behaviors across the migration.

### Risks
- **Falsifier:** after the router lands, a mechanical coordination step (dispatch, deps,
  merge, drain, teardown) still requires an agent TURN — i.e., the router delegates routing
  to an LLM — OR a routing bug of the old class (double-dispatch, a missed event-wake, a CI
  mis-rollup) recurs in the router (it should be impossible by construction: idempotent
  DB-backed handlers + a queue consumer). Equally: judgment that belongs to a worker or the
  validator is absorbed into the router, re-introducing the substrate this ADR removed.
- **Second serialization point:** a single validator handling N PRs is itself a throughput
  limit (named in ADR-0117 Risks). The router invokes the validator per PR, so it CAN fan
  out validators; the migration must not replace one serial coordinator with one serial
  validator. (Validator fan-out policy is a follow-up, not this decision.)

## Diagram

```mermaid
sequenceDiagram
    participant Router as Router (server code — no turn)
    actor Worker
    actor Peer as Peer worker
    actor Validator
    participant Branch as joes-agents/&lt;slug&gt;
    Router->>Worker: dispatch task (deps satisfied)
    Worker->>Router: PR opened
    Router->>Peer: dispatch peer review (§8)
    Peer->>Router: buy-in
    Router->>Validator: request verdict (per PR, fan-out-able)
    Validator->>Router: pass | rework
    Router->>Branch: git merge + push (on pass)
    Router->>Router: resolve depends_on, dispatch now-unblocked tasks
    Note over Router: idempotent DB handlers — no turn to grow, cannot double-dispatch
```

## Follow-ups

- **Migration plan (owned by the coordinator-code owner):** sequence the move — (1) dispatch
  + `depends_on` resolution + `task_prs` bookkeeping into the router; (2) integration merge +
  drain + teardown; (3) retire the coordinator template's mechanical sections. Each phase
  preserves ADR-0114/0115/0116 behavior and ships behind a per-repo flag so a repo can run
  the router or the legacy coordinator during cutover. This ADR sets the decision and the
  split; the plan sets the sequence.
- **Inventory:** confirm exactly which coordination already lives in server code (scheduler,
  drain-guard endpoint, mergeability views) versus only in the template, so the router
  consolidates rather than rebuilds. (Do not assume; verify against the code.)
- **Validator fan-out:** decide whether the router runs validators in parallel per PR
  (removing the second serialization point), as a separate ADR.
- **ADR-0117** remains the live interim mitigation until phase 1 lands, then is retired.

## References

- ADR-0087 (the coordinator-as-agent core this supersedes; workers + validator + peer review
  kept), ADR-0117 (interim per-turn cap), ADR-0108/0116 (validator + gate weight),
  ADR-0110/0114 (feature-branch + integration base the router must preserve), ADR-0115 (the
  double-dispatch — an example of the bug class the router eliminates).
- The 2026-09-14 dogfood learnings: single-coordinator serialization; the
  observer→coordinator wake gap; the CI multi-suite/`skipped` mis-rollup.
- `tools/team-templates/coordinator/CLAUDE.md.tmpl` (the prose state machine this replaces
  with router code).
