---
auto_merge: false
---

# Plan: Coordinator-router-in-code (land ADR-0118)

- **Status:** drafting
- **Date:** 2026-09-15
- **Related ADRs:** ADR-0118 (coordinator is a router), ADR-0087 (superseded core), ADR-0117 (interim cap, retired at phase 1), ADR-0114/0115/0116 (behaviors the router must preserve), ADR-0108/0116 (validator).

## Goal

Replace the coordinator **agent** with a **router in server code**. Move all mechanical
coordination (dispatch on dependency-satisfaction, `depends_on` resolution, `task_prs` and
task-state bookkeeping, integration `git merge + push`, drain-guard, standup/teardown) into
deterministic, idempotent, DB-backed handlers. Agent turns are reserved for judgment:
workers (implement, peer-review, conflict-resolve) and the validator (verdict). This deletes
by construction the coordinator-DISPATCH serialization/wedge and the routing bug class
(double-dispatch, missed event-wake, CI mis-rollup). It does NOT remove the validator
serialization point — a single serial validator remains; validator fan-out is a separate ADR,
out of scope here (review Non-blocking 4). Operator directive 2026-09-15 (Joe, direct): "The
coordinator shouldn't be an agent."

## Success criteria

Observable, each judgeable at the end:
1. A team runs a full plan end-to-end (dispatch → PR → peer review → validator → integration
   merge → `depends_on` release → teardown) with **zero coordinator agent turns** — no
   `coordinator-<repo>` Claude session is launched for a router-mode repo.
2. **Wake-gap cannot recur — measured on the DISPATCH RECORD, not idle-inference**
   (Donna + Carla correction 2026-09-15: "idle + downstream-complete" is an AMBIGUOUS
   wake-gap signal — the coordinator may have ALREADY dispatched and be legitimately waiting;
   a real wake-gap is idle AND the next step NOT dispatched). The falsifier, per OBSERVED
   trigger, keys on the deterministic appearance of a dispatch / `task_execution` row:
   - (a) `depends_on`-satisfied event delivered → a dispatch row for the now-unblocked task.
   - (b) rework / peer_review VERDICT delivered → a next-cycle dispatch row.
   - (c) required-CI-terminal + passing delivered (IGNORING non-required / `unstable`) → a
     re-eval dispatch row. (`unstable` from a non-required `neutral`/`skipped` is NOT a
     failure — never block or wait-forever on it.)
   For each: **RED on the LEGACY path** (no server-side consumer acts on the delivered event —
   only the agent turn creates the row, which Phase-0 confirmed is absent while the turn is
   idle), **GREEN on the router** (the row appears deterministically). Demonstrate
   RED-on-legacy FIRST — a falsifier that cannot fail on the coordinator that actually failed
   is vacuous. This dispatch-record assertion is a NON-ambiguous falsifier and is exactly the
   delivered-but-no-consumer gap Phase-0 found; it replaces the earlier "idle → nudge" signal.
   - The 2 logged traces (TRACE 1 dispatch-gap `5d1e1da`; TRACE 2 ci_result `22210e79`) are
     **MOTIVATING, not confirmed** — TRACE 1 is the stronger candidate (stuck head, zero
     worker activity); both need a dispatch-record check before they stand as confirmed
     wake-gaps (Carla's own false-positive on the settled-fix head `947e781b` showed
     idle+CI-complete alone is not a gap).
3. **Idempotency is cycle/generation-scoped, not task-only or event-only** (review Blocking
   3). A re-delivered event dispatches at most once WITHIN a cycle, but a genuine new rework
   cycle IS allowed to re-dispatch the same task. Test both: (a) a duplicate event in one
   cycle → one dispatch; (b) a legitimate next cycle → a new dispatch (a task-only key would
   wrongly dedup this into a fresh stall). Key = `(task_id, generation)`, the
   emit_version-per-generation pattern from the run_shape contract.
4. **CI rollup correctness — DISTINCT from the SC2c wake** (Carla; both required). Required
   vs non-required is computed right: required={all `success`} + non-required={`neutral`,
   `skipped`} (mergeable_state=`unstable`) rolls up to PASS/ready — the rollup NEVER treats
   `unstable` from a non-required neutral/skipped as not-ready or as failure. Unit test, not
   prose. (SC2c is "did terminal trigger the re-eval"; SC4 is "did the set roll up correctly"
   — the observed #22845 gap was the WAKE, not a mis-rollup, so both are tested.)
5. **Preserved behaviors — RED-then-GREEN foils** (review Non-blocking 5): ADR-0114 per-plan
   `integration_base` (incl. chained base), ADR-0115 feature-branch self-drive + inline
   `depends_on`, ADR-0116 gate-weight by `merge_target` — each has a foil that goes RED when
   the behavior is broken and GREEN when restored, against the router path (not merely a
   passing test).
6. **Substrate binds PER-PLAN at standup, immutable for the plan's life** (review Blocking 2
   — a per-repo flag flipped mid-plan would run legacy AND router on the same repo → both act
   on one event → cross-substrate double-dispatch / split-brain). A plan records its owning
   substrate (`legacy` | `router`) at standup; the flag is resolved ONCE at standup and never
   re-read per event; the router ignores legacy-owned plans and the legacy coordinator
   ignores router-owned plans. Foil: flip the repo flag while a plan is in flight → assert the
   running plan's substrate is UNCHANGED and only ONE substrate acts on its events.

## Constraints / scope

### In scope
- Router handlers for the mechanical coordination surface (phases 1–2 below).
- A per-repo flag that selects the substrate for NEW standups, **bound per-plan and
  immutable at standup** (SC6): the repo flag decides which substrate a plan is created
  under; each plan captures that once and never re-reads it, so a mid-plan flip cannot create
  a split-brain.
- Retiring the coordinator template's mechanical sections and ADR-0117 (phase 3).
- Acceptance tests encoding the falsifier (no agent turn for a mechanical step; the routing
  bug class impossible by construction).

**Execution model (review Non-blocking 7):** the `sequence_of_work` below is authored as
prose for review; when submitted it is converted to machine plan-tasks for a worker team
(server-code build with tests). If instead the coordinator-code owner drives it directly, the
prose sequence stands as the driver checklist. Chosen at submit; Joe's call on team-vs-direct.

### Out of scope
- **Validator fan-out** (parallel validators per PR) — 0118 defers it to a separate ADR;
  this plan invokes the validator per PR as today and does NOT change its serialization.
- **The defect-finding gate rigor** (evaluator, peer review, rework) — untouched. This plan
  changes DISPATCH, not judgment.
- **Brief/context assembly** — stays the context service's job, not the router's.
- **Mid-flight cutover of a running team** — in-flight plans finish on their current
  substrate; no live migration of Team A P2 or Team B #22845.

### Budget
Sequenced build across the three phases; each phase ships behind the flag and is
independently revertable. If phase 1 cannot preserve ADR-0114/0115/0116 behavior behind the
flag, stop and re-scope rather than cut over.

## Sequence of work

- **Phase 0 — Inventory — DONE (2026-09-15).** Verified against
  `services/api/treadmill_api/` + `tools/team-templates/coordinator/CLAUDE.md.tmpl`. Result:
  the server is a control plane that persists facts, resolves attribution, and DELIVERS
  events — but has NO dispatch decision/execution (the old `dispatch_task` was deleted in
  ADR-0087 phase 4–5; the two in-process event subscribers are pure push transports).
  - **Already server-side → the router just calls these seams (consolidate):**
    task_prs registration + task_execution transitions (`routers/task_prs.py:79`,
    `routers/task_executions.py`); drain-guard quiescence (`routers/scheduler.py:142`
    `is_quiescent()`); CI suite rollup (`ci_observer.py:120` `maybe_emit_ci_result`,
    idempotent on `(head_sha, check_suite_id, conclusion)`); webhook/poll ingress
    (`webhooks/persist.py:84`) + team-activation scheduler + WS/fabric event DELIVERY
    (`routers/dashboard/ws.py:433`, `coordination/fabric_event_sink.py`); mergeability view
    + endpoint (`task_mergeability` VIEW, `routers/tasks.py`); lifecycle state + teardown
    enactment (`team_scheduler.py`).
  - **Template-only → net-new router build:** (1) the **event→dispatch consumer** — nothing
    server-side subscribes to `task.ci_result` / `github.pr_merged` / `task.completed` /
    `task.evaluator_verdict` and ACTS; today only the coordinator agent's turn does (template
    §3.3/§3.4/§6.1/§9.3), which IS the wake-gap; (2) the `depends_on` **satisfaction
    evaluator + fan-out** (grammar/storage exist at `routers/plans.py:213`,
    `task_dependencies`, but nothing evaluates "edge satisfied" then dispatches); (3)
    **integration git merge/push + feature-branch standup/preflight** (template §3.1a,
    §9.3-feature — deterministic shell git, prose only); (4) **gate-position branching by
    `merge_target`** (ADR-0116 — field stored at `team_config_store.py:37`, decision is
    prose §3.5).
  - The single highest-value piece is the **event→dispatch consumer**: delivery already
    works and is idempotent; nothing acts on it without an agent turn. Adjudication stays an
    agent — the server records `task.evaluator_verdict`; the router consumes it mechanically
    (approve→integrate/dispatch, rework→re-dispatch author), no judgment moved.
- **Phase 1 — Dispatch + deps + bookkeeping into the router (per-plan substrate).**
  The **event→dispatch consumer** (the wake-gap fix): a server-side subscriber consumes each
  delivered task-scoped event — `github.pr_merged`, `task.ci_result`, `task.evaluator_verdict`
  (verdict=rework → re-dispatch author; verdict=approve → integrate/dispatch), `run.completed`,
  `task.completed` — resolves `depends_on`, and dispatches the now-unblocked task. Register
  `task_prs`; own task-state transitions. Router MUST specs (blocking, from review):
  - **Idempotency key `(task_id, generation)`** — a re-delivered event dispatches at most once
    within a cycle; a genuine new rework cycle re-dispatches.
  - **CI-terminal readiness:** compute "required checks terminal" IGNORING non-required; fire
    re-eval on required-terminal+passing; treat `unstable` (non-required `neutral`/`skipped`)
    as NOT-a-failure — never block or wait-forever (the exact #22845 case).
  Idempotent DB-backed handlers with unique constraints. Retire ADR-0117's per-turn cap when
  this lands.
- **Phase 2 — Integration + lifecycle into the router.** Integration `git merge + push` to
  the per-plan branch (ADR-0110/0114 base rules), the drain-guard, standup/teardown
  (ADR-0109), gate-position by `merge_target` (ADR-0116). Preserve ADR-0115 self-drive.
- **Phase 3 — Retire the agent coordinator.** Remove the template's mechanical sections;
  retire ADR-0117; document the router as the coordination substrate. A repo with no
  in-flight plan flips to router-only.

## Diagram

See ADR-0118's sequence diagram (the router/worker/validator interaction is the contract of
intent for this plan; not duplicated here).

## Risks / unknowns

- **Hidden judgment in the prose.** A behavior that reads as mechanical may encode a real
  judgment call (when to escalate, conflict triage). Mitigation: Phase 0 flags each; anything
  needing judgment routes to a worker or the validator — never back into the router as prose.
  Abort-trigger: if a core dispatch step cannot be made deterministic without an agent
  decision, stop and raise an ADR amendment.
- **Behavior drift across migration.** The router must preserve ADR-0114/0115/0116 exactly.
  Mitigation: success-criterion 5 regressions run against the router path before any repo
  cuts over.
- **Second serialization point (validator).** Out of scope here, but the migration must not
  replace one serial coordinator with one serial validator silently — note it and hold for
  the fan-out ADR.
- **Evidence base (regression targets for criterion 2), from Carla's logged traces
  2026-09-15:**
  - TRACE 1 — dispatch gap (bitballoon #22845, task 4ec4f3b3, cycle-3): head stuck at
    5d1e1da from 06:24:52Z; at 06:41:22Z still 5d1e1da (~16.5 min stale), all 3 sessions
    idle — cycle-2's cleared rework verdict never dispatched cycle-3; resolved only by a
    manual nudge → c00f07eb @ 06:41:40Z. Foil: a cleared rework verdict MUST deterministically
    dispatch the next cycle, no nudge (RED on legacy, GREEN on router).
  - TRACE 2 — ci_result wake gap (same PR, head 22210e79 @ 06:57:37Z): all 6 CI check-runs
    complete by ~06:58; at 07:02:21Z coordinator idle ~4 min, CI-complete→re-eval never
    fired; resolved by a manual nudge. Foil: CI reaching terminal MUST deterministically
    trigger the re-eval, no nudge (RED on legacy, GREEN on router).
  - Two further recurrences this session (part2 slice-1 ci_result; ARO evaluator-dispatch)
    are named as pattern; exact SHAs to be recovered from the transcript before encoding.
  Both precise traces are the same class: the coordinator did not wake on a task-scoped
  event (dispatch / ci_result) — encode them as the criterion-2 red/green foils.

## Decisions captured during execution

- 2026-09-15, from the Donna + Carla adversarial review (3 blocking, folded pre-submit):
  - **SC2 is per-observed-trigger** (depends_on-dispatch, rework-verdict re-dispatch, ci_result
    re-eval), each a red-on-legacy foil on a precise trace — not a generic event.
  - **Substrate binds per-plan, immutable at standup** (not per-repo per-event) — closes the
    cross-substrate double-dispatch / split-brain a mid-plan flag flip would cause.
  - **Idempotency key is `(task_id, generation)`** — dedups a re-delivered event within a
    cycle yet ALLOWS a genuine new rework cycle (a task-only key would stall the rework).
  - Goal tightened to coordinator-dispatch serialization only (validator fan-out out of
    scope); SC5 made red-then-green; execution model clarified.

## Post-mortem

(filled on completion/abandonment)
