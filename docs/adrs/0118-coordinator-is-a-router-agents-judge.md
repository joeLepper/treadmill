# ADR-0118: The coordinator is a router (code); the agents that judge are the workers and the validator

- **Status:** accepted (amended by ADR-0119 — integration executes host-side as the operator)
- **Date:** 2026-09-14
- **Amends:** ADR-0087 (long-lived team execution model) — supersedes its *coordinator-as-agent* core; the worker + validator + peer-review model it defines is kept.
- **Supersedes:** ADR-0117 (coordinator yields between PRs) — that per-turn cap is an interim bridge, retired only at the end of the migration (below), not on proposal.
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

1. **The coordinator is an LLM-turn serialization point that can wedge.** On an N-parallel
   plan the single coordinator processed all PRs' review/rework/verdict traffic serially
   inside one agent turn (~40k tokens, ~10 minutes), growing toward the context/time limit
   mid-orchestration — a wedge risk on a client production repo (ADR-0117). Workers sat idle
   behind the serial turn.
2. **A recurring class of routing defect lived in the coordinator's prose.** The
   double-dispatch of a dependent (ADR-0115), the observer→coordinator event that never woke
   the coordinator, and the multi-suite / `skipped` CI mis-rollup (ADR-0116) were each a
   mechanical decision encoded as fallible English in a template, caught in review by
   verifying the mechanism against the claim.

The root observation: the coordinator's job is almost entirely **mechanical** — dispatch a
task when its dependencies are satisfied, register its PR, run the integration merge, apply
the drain-guard, resolve `depends_on`, stand up and tear down. An agent turn is a poor
substrate for that: it has a finite context (hence the serialization and the wedge), and it
runs a state machine as prose (hence a class of defect that is invisible until it fires).

The **judgment** in the team model lives in the **workers** (how to implement a task;
whether a sibling's PR is sound in peer review; how to resolve a merge conflict) and in the
**validator** (the review verdict, panel-backed per ADR-0108/0116). The coordinator's own
decisions are mechanical routing — with three specific exceptions that today ride on the
coordinator agent's judgment (§Decision), each of which can be re-homed without an agent in
the coordinator seat.

## Decision

We decided the **coordinator is a router implemented in server code, not an agent.** All
mechanical coordination moves into the Treadmill router; agent turns are reserved for
judgment, and the judgment agents are the **workers** and the **validator**. The router
invokes those agents and reads their structured, revision-scoped results; it holds no agent
turn of its own.

- **The router (server code) owns:** task dispatch on dependency-satisfaction; `depends_on`
  resolution; `task_prs` and task-state bookkeeping; the integration `git merge + push` to
  the per-plan branch (ADR-0110/0114 base rules preserved); the drain-guard and
  teardown/standup lifecycle (ADR-0109); sequencing across a plan's tasks; and the
  gate-position choice (ADR-0116 breadth by `merge_target`).
- **The workers (agents) own:** implementing a task; peer-reviewing a sibling's PR (the
  ADR-0087 §8 inner loop, kept); and resolving a merge conflict when the router routes one
  as a task.
- **The validator (agent) owns:** the review verdict — the panel-backed evaluation and its
  rework signal (ADR-0108/0116), invoked by the router per PR, and (see below) the tiebreak
  on a contested peer verdict.

**Three judgments the coordinator agent makes today, and where each goes** (naming these is
what makes "no irreducible judgment in the coordinator" true rather than asserted; the
migration plan implements them):

- **Trivial-vs-task conflict triage** (tmpl §9.3: "a textual conflict you can resolve
  trivially → resolve+push, else open a task"). A router cannot judge "trivially
  resolvable." Resolution: a **deterministic** is-trivial rule (git `rerere` + a
  non-overlapping-hunk auto-merge), and every conflict that rule does not clear is dispatched
  as a worker task. If we decline the deterministic rule, the fallback is "all conflicts →
  tasks," which is a behavior change (it loses today's inline trivial-resolve).
- **A contested peer verdict** — the sharp one. The template's collation is already
  mechanical (any `needs-changes` → rework), BUT on this dogfood (#1215) the coordinator
  agent HEAD-verified that a worker's `needs-changes` was FALSE and declined to rework. A
  pure router mechanically reworks on any `needs-changes`, so a false verdict costs a full
  cycle. **Decision: route a CONTESTED verdict (author disputes the `needs-changes`) to the
  VALIDATOR as tiebreak** — preserving the correctness the coordinator's HEAD-verify
  provided, in an agent that is already a judgment seat. (The simpler alternative — accept
  the extra cycle, author re-argues next round — is a real behavior change we are choosing
  NOT to take.)
- **Novel-failure diagnosis** — on this dogfood the coordinator agent root-caused a
  stale-GitHub-API-download worker bug and corrected the workers. A router escalates on
  mechanical triggers but cannot diagnose a novel failure. Resolution: the router escalates
  to the plan's `created_by` (the exec-in-charge) or dispatches a diagnostic task; the
  diagnosis is not the router's.

**The seam (requires NEW integration — it is not an existing dispatcher).** At this commit
the pre-ADR-0087 `dispatch_task` is GONE (`services/api/treadmill_api/dispatch.py`: the
workflow/step tables were dropped; the module now only persists+publishes Events), and
`POST /task_executions` commits a `running` row but does NOT deliver a worker brief — the
coordinator template separately POSTs that row and then sends the cc-relay
(`tmpl` §5). So the router's **worker/validator invocation + result-correlation is a genuine
new build**, not a re-point of an existing job dispatcher. What IS real reuse: the scheduler
module, `resolvers.py`, the drain classification (`routers/team_configs.py`), and the
mergeability machinery all exist server-side — the router consolidates and extends those for
the plumbing. The orchestration loop itself (the `depends_on`-resolution loop, the
integration merge+push, the review/validator sequencing) is coordinator-PROSE today and is
the real build.

## What the router substrate does and does NOT eliminate

Moving coordination into code removes the failures that came FROM the agent substrate: the
**LLM-turn/context-limit serialization and wedge**, and the **prose-execution fragility**
(a state machine that only exists as English). It does NOT, by itself, make the
double-dispatch / lost-event / wrong-rollup class impossible — those are ordinary
distributed-systems and logic defects that a router can still have:

- A DB unique constraint prevents duplicate *rows*, not duplicate or lost *deliveries*: the
  commit-then-send (or send-then-commit) dual-write can still drop a dispatch on a crash
  after commit, or double-deliver on a retry after send. **The router must carry durable
  delivery intent + reconciliation, a stable execution identity, and destination-side dedup
  (or honest at-least-once execution),** with foils for the crash-in-between cases.
- Determinism does not fix a wrong aggregation *rule*: a last-event CI reducer green-lights a
  required-suite failure followed by an optional-suite success. **The rollup must be
  head-/suite-aware**, with foils.
- A validator `pass` must be **tied to the execution/head/base revision it judged**: a stale
  `pass` after a new push must NOT authorize the new head.

So the correct claim is: the router **eliminates the agent-turn failure class and the
prose-execution class**, and turns the routing-logic defects from prose-hidden into
ordinary, testable software defects with standard fixes — not that code makes them
impossible. ADR-0117's per-turn cap is an interim bridge (retired at the end of the
migration, below).

## Alternatives considered

- **Incumbent: the coordinator agent (ADR-0087).** Why insufficient: it serializes
  N-parallel work in one growing turn (a prod-repo wedge risk, observed live) and encodes a
  mechanical state machine as agent-prose (a routing-defect class caught repeatedly in
  review). The judgment that would justify an agent lives in the workers and the validator;
  the coordinator's three judgment residues (above) can each be re-homed.
- **Bound the coordinator's turn (ADR-0117: per-turn cap + yield).** Rejected as the durable
  fix: it treats the symptom (turn growth) but keeps the agent substrate, so the
  prose-execution fragility and the serialization root remain. Kept as the interim bridge.
- **Shard into multiple coordinator agents per repo.** Rejected: ADR-0087 made one
  coordinator the single owner to prevent two writers racing `task_prs` and integration
  order. A router is single-writer for DB state, but note it does NOT by itself serialize
  Git effects (below) — sharding agents would add the race back without fixing that.
- **Keep some judgment in the coordinator (brief/context assembly, conflict triage).**
  Rejected per the operator's scoping (2026-09-14): brief/context assembly is a service/tool
  (the context service already compiles briefs); the three judgment residues are re-homed as
  above. The coordinator need not be an agent.

## Consequences

### Good
- No agent turn to grow: the coordinator's LLM-turn serialization and mid-orchestration
  wedge are removed. (Throughput then depends on worker/validator fan-out — a deferred
  follow-up — and on the Git-integration ordering below; this ADR does not claim the
  fan-out throughput win it defers.)
- The routing-defect class (double-dispatch, missed event-wake, CI mis-rollup) moves from
  prose-hidden to deterministic, testable code — provided the router adopts the delivery /
  identity / dedup / rollup discipline named above (an implementation obligation, not
  automatic).
- Consolidates coordination logic that already exists server-side (scheduler, `resolvers`,
  drain classification, mergeability), ending the coordinator agent's prose duplication of
  it. The orchestration loop is a real new build (owned honestly below).
- Agents do only what needs judgment (implement, peer-review, validate) — each
  independently parallelizable.

### Bad / trade-offs
- A real build, not a prose edit: the worker/validator invocation + result-correlation is
  NEW integration (the old `dispatch_task` is gone); plus the `depends_on` loop, the
  integration merge, migrations, and a migration off the coordinator template. Large blast
  radius — a sequenced, staged plan (below).
- **Same-branch Git integration is itself a serialization/ordering point the DB does not
  remove.** Serialized DB writes do not serialize Git effects: two merges from the same base
  can leave one push rejected and needing recovery/retry. The router must order (or CAS)
  integration pushes per branch; "no serialization at all" would be false.
- The three judgment residues (conflict triage, contested verdict, novel-failure diagnosis)
  are behavior-relevant: the contested-verdict tiebreak (routed to the validator) and the
  novel-failure re-homing (escalate/diagnostic-task) change where those decisions are made.
  Named here deliberately, resolved in the migration plan.

### Risks
- **Architecture falsifier:** after the router lands, a mechanical coordination step
  (dispatch, deps, merge, drain, teardown) still requires an agent TURN — i.e., the router
  delegates routing to an LLM — OR a judgment that belongs to a worker/validator is absorbed
  into the router. Either means the substrate split failed.
- **Implementation obligations (acceptance tests for the migration, NOT claims of this
  ADR):** the router must demonstrate, with foils, durable-delivery + reconciliation (no
  dropped/duplicated dispatch across a crash between DB commit and send), a stable execution
  identity + destination dedup, head/suite-aware CI rollup, revision-scoped validator
  verdicts, and per-branch integration ordering. "Cannot double-dispatch by construction" is
  true only if this discipline is actually used.
- **Cutover split-brain:** an already-running old coordinator agent and the new router both
  acting on one repo double-deliver. A config flag alone cannot stop a running old agent, so
  the migration requires a FENCED per-repo ownership transfer + recovery (the old agent is
  provably stopped/fenced before the router assumes the repo). Until every affected
  coordinator agent is retired from review/verdict/integration turns, ADR-0117's per-turn cap
  MUST remain.

## Diagram

```mermaid
sequenceDiagram
    participant Router as Router (server code — no turn)
    actor Worker
    actor Peer as Peer worker
    actor Validator
    participant Branch as joes-agents/&lt;slug&gt;
    Router->>Worker: dispatch task (deps satisfied; durable intent + stable exec id)
    Worker->>Router: PR opened (head/base revision)
    Router->>Peer: dispatch peer review (§8)
    Peer->>Router: verdict
    Router->>Validator: request verdict (per PR; tiebreak a contested peer verdict)
    Validator->>Router: pass@revision | rework
    Router->>Branch: git merge + push (ordered/CAS per branch; on pass@head)
    Router->>Router: resolve depends_on, dispatch now-unblocked tasks
    Note over Router: idempotent DB handlers + durable delivery — routing defects become testable, not impossible
```

## Follow-ups

- **Migration plan (owned by the coordinator-code owner):** sequence the move — (1) dispatch
  + `depends_on` resolution + `task_prs` bookkeeping into the router; (2) the worker/validator
  invocation + result-correlation seam (new build); (3) integration merge + drain + teardown;
  (4) retire the coordinator template's mechanical sections. Each phase preserves
  ADR-0114/0115/0116 behavior, ships behind a per-repo FENCED ownership transfer (not a bare
  flag), and resolves the three judgment residues explicitly (deterministic conflict rule;
  validator tiebreak on a contested verdict; escalation/diagnostic-task for novel failures).
- **ADR-0117** remains the live interim mitigation until the LAST phase removes every
  coordinator agent from review/verdict/integration turns — then it is retired. Removing the
  cap earlier leaves the long-turn risk on whatever the legacy agent still drives.
- **Inventory:** the plan confirms exactly which coordination already lives in server code
  versus only in the template, so the router consolidates rather than rebuilds where it can.
- **Validator fan-out:** whether the router runs validators in parallel per PR (removing the
  validator as the next serialization point) is a separate ADR.

## References

- ADR-0087 (the coordinator-as-agent core this supersedes; workers + validator + peer review
  kept), ADR-0117 (interim per-turn cap), ADR-0108/0116 (validator + gate weight),
  ADR-0110/0114 (feature-branch + integration base the router must preserve), ADR-0115 (the
  double-dispatch — an example of the routing-defect class, which the router makes testable,
  not impossible).
- The 2026-09-14 dogfood learnings: single-coordinator serialization; the
  observer→coordinator wake gap; the CI multi-suite/`skipped` mis-rollup.
- `services/api/treadmill_api/dispatch.py` (the removed `dispatch_task`; the surviving
  durable-event seam), `routers/task_executions.py` (commits a row, delivers no brief),
  `tools/team-templates/coordinator/CLAUDE.md.tmpl` (the prose state machine this replaces).
