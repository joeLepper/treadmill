---
auto_merge: false
---

# Plan: Team lifecycle + feature-branch integration (v1)

- **Status:** drafting
- **Date:** 2026-09-13
- **Related ADRs:** ADR-0109 (ephemeral team lifecycle), ADR-0110 (feature-branch integration), ADR-0087 (team execution), ADR-0108 (panel-backed evaluator)

## Goal

Build the v1 team lifecycle manager + feature-branch integration the two ADRs decided:
teams that stand up on demand, integrate on a per-plan `joes-agents/<slug>` branch, and
tear down cleanly when the plan is done — so idle teams stop burning resources and teams
run unfettered on repos that forbid agent-merge-to-main. Cross-model workers are OUT
(their own capacity-gated ADR).

## Success criteria

1. The drain-guard blocks teardown on work the TEAM is actively doing, NOT on work PARKED
   ON A HUMAN. `treadmill team down <slug>` stops + disables every team unit and keeps the
   rendered dir; it REFUSES (manual) or WAITS (auto) while a task is TEAM-ACTIVE
   (`registered|assigned|executing|pr_open|rework-pending`, or a merged task still under
   post-merge observation) or a task PR into the integration branch is open. It does NOT
   block on **parked-on-human** items — an `escalated` task (awaiting the operator) or the
   `branch → main` handoff PR (awaiting the operator's merge). Those are tracked so the
   team RE-STANDS-UP on the human's action; leaving one open never pins the team. (This
   refines ADR-0109's drain-guard set — panel-found, see Decisions.)
2. A per-repo team config carries `lifecycle` (ephemeral|persistent|manual) and
   `merge_target` (feature-branch|main); both are honored.
3. Standup claims an ATOMIC per-repo lease — two concurrent `plan.submitted` for one repo
   yield ONE team, and the LOSING submit ATTACHES its plan to the standing team (never
   queued-away, never dropped) (foil: fire two, assert one coordinator AND both plans
   attached).
4. Feature-branch mode: standup creates `joes-agents/<slug>` off main and a PREFLIGHT
   verifies the agent can integrate there (test push); if not, standup FAILS LOUD +
   escalates (foil: no-push-access repo → loud fail, not a silent stall). Task PRs base
   onto the branch; the coordinator integrates approved tasks by `git merge + push`.
5. "Implemented" = all tasks integrated AND the `branch → main` handoff PR opened +
   recorded on the plan + SURFACED to the operator (an event). Teardown fires after that —
   the open handoff PR is parked-on-human (criterion 1), so it does NOT re-block teardown.
   Ephemeral teardown also fires on all-terminal-failure. Idle-grace prevents thrash.
6. Idle-sweep tears down any team idle > N hours that passes the drain-guard.
7. FOILS — red-then-green, not prose (the drain-guard is the safety invariant, so a
   test that checks only `executing` while `post-merge-observation` ships unguarded is
   the exact gap to avoid):
   - **Drain-guard PER STATE:** `team down` REFUSES (red) with a task in EACH of
     `registered` / `assigned` / `executing` / `pr_open` / `rework-pending` /
     post-merge-observation, and with any open TASK PR; SUCCEEDS (green) when all are
     terminal. **Parked-on-human is green-WITH-tracking:** an `escalated` task or an open
     `branch → main` handoff PR lets teardown proceed, and TWO safety foils make parking
     not-lose-work and not-strand (Ernie): (a) **work durability** — escalate a task whose
     worker had in-progress UNCOMMITTED work → teardown → re-standup → assert the WORK
     (branch content, not just the task row) survives; the state machine must commit/push
     on-escalate so `escalated` never coexists with uncommitted worker state; (b)
     **re-standup FIRES on the human's response** — park an escalation → simulate the
     operator's escalation-RESPONSE → assert the team actually re-stands-up (reconcile
     acts on the response, not only on `plan.submitted`, else a resolved escalation
     strands). So a stuck escalation never pins the team, yet its work is never lost and
     its resolution always resumes.
   - **Handoff-before-teardown NEGATIVE:** reach "all tasks integrated" WITHOUT an
     opened+recorded+surfaced handoff → teardown MUST NOT fire (red); with it → fires (green).
   - **Mode red halves:** `persistent` → NOT auto-torn-down / NOT swept; idle-but-in-flight
     → NOT swept.
   - Lease: two concurrent submits → one coordinator AND both plans attached. Preflight:
     no-push repo → loud fail, not silent stall.
   - **Scope the touched-component tests + docs** (or the docs-current/test gates red the
     PR): coordinator template tests (`test_coordinator_template.py`) + its `AGENT.md`;
     `install.py` tests; the new watcher's unit tests + an `AGENT.md` (it is new
     long-running infra).

## Constraints / scope

### In scope
- `team down` + drain-guard; per-repo mode config; atomic standup lease; ephemeral
  auto-standup/teardown watcher + reconcile; idle-sweep; feature-branch create + preflight
  + coordinator git-push integration + handoff PR/surface. Coordinator + install.py +
  a lifecycle watcher.

### Out of scope
- Cross-model workers (separate capacity-gated ADR).
- Changing the evaluator (already panel-backed, ADR-0108) or the review discipline.
- **PRESERVED, must not be dropped:** the ADR-0087 §8 sibling-worker peer-review inner
  loop (1–2 peer workers buy in on a PR BEFORE it reaches the evaluator) and the
  intra-team fabric messaging (workers ↔ peer workers ↔ coordinator via `send`). The
  build modifies the coordinator template (git-push integration + handoff), so a
  regression test must assert the re-rendered coordinator/worker templates STILL carry
  §8 peer review. Order stays: PR → §8 sibling buy-in → §9 evaluator (panel-backed
  cross-model) → integrate. The panel is the cross-model layer AFTER same-family buy-in,
  never a replacement for it.
- The `main`-merge-target path beyond leaving ADR-0087's behavior for permissive repos.

### Budget
Operator-team work (I drive; direct edits). Dogfood clarified: the branch WORKFLOW
(work on `joes-agents/*` + a manual `branch → main` PR) is just git + ADR-0110's human
path — available TODAY, used now. The automated TOOLING (standup preflight, coordinator
git-push integration) can't be dogfooded until steps 3–4 build it; it is proven in step
5 (one real team end-to-end). Abort to a post-mortem if the drain-guard can't be made safe.

## Sequence of work

1. **`team down` + drain-guard** (the standalone primitive): the manual primitive + the
   per-state in-flight check + the per-state foils. Ships alone, depends on nothing
   downstream.
2. **Per-repo mode config + idle-sweep**: config schema, honoring, the sweep + tests.
   **Step 1+2 together are the incident-fix** — idle teams auto-cleaned, no per-team
   manual invocation (the actual 25-idle-teams remedy). Steps 3–4 are independently
   sequenced.
3. **Feature-branch integration**: branch create, standup preflight (fail-loud),
   coordinator git-push integration, the `branch → main` handoff PR + surface event.
4. **Ephemeral watcher + atomic lease**: standup on `plan.submitted`, teardown on
   done/terminal, reconcile, the per-repo lease + race foils.
5. **Review + deploy**: panel + a verifier (prefer Fran) on the changes; stand up one
   real ephemeral team on a feature-branch repo end-to-end; report.

## Risks / unknowns

- The lifecycle watcher's home (daemon vs coordinator-adjacent vs periodic reconcile) —
  spike in step 4; it MUST make admission/teardown atomic per repo (the lease).
- Coordinator `git merge main` conflict handling (ADR-0110): trivial auto-resolve else a
  reviewed conflict task — verify it doesn't wedge.
- We abort if teardown can strand in-flight work or a Go/infra dependency wedges standup.

## Decisions captured during execution

- **Drain-guard distinguishes TEAM-ACTIVE from PARKED-ON-HUMAN** (panel review of this
  plan). Blocking on `escalated` or the open `branch → main` handoff PR forever would pin
  the team and defeat the resource goal — both are work parked on a HUMAN, not the team.
  They do NOT block teardown; they are tracked so the team re-stands-up on the human's
  action. This refines ADR-0109's drain-guard set (which listed `escalated` as blocking)
  — recorded as an amendment note on ADR-0109. The blocking set is now team-active only:
  `registered|assigned|executing|pr_open|rework-pending` + post-merge-observation + open
  task PR.
- **The drain-guard is a MULTI-SOURCE join** (Ernie pre-code flag, confirmed against the
  schema). Post-merge observation is NOT in the task's `derived_status` (which tops out at
  `pr_merged`) — it is a SEPARATE `deploy`/`staging_smoke` EVENT stream
  (`services/api/treadmill_api/events/deploy.py`). So a task at `pr_merged` whose
  deploy/smoke is unsettled is still in-flight (coordinator owns rollback), and keying on
  `derived_status == pr_merged → allow` — or reusing the server's
  `_in_flight_task_executions_for_labels` (`status='running'`, no post-merge notion) —
  reopens the gap. The guard must join THREE sources: `task_status.derived_status`
  (team-active states block), `deploy`/`staging_smoke` observation (merged-but-unsettled
  BLOCKS), and `task_prs` (open team-authored task PRs block). The post-merge foil MUST
  run against this real join, never a mock hard-coding `pr_merged → block`. Likely a small
  server endpoint computes the join (extend the scale-down guard); the CLI `team down`
  calls it.

## Post-mortem

(pending)
