# Plan: ADR-0087 implementation — long-lived team execution model

- **Status:** completed
- **Date:** 2026-06-10
- **Related ADRs:** ADR-0087
- **Authors:** treadmill-alan, treadmill-bert, treadmill-carla

## Goal

Ship the execution model described in ADR-0087: kill the autoscaler / SQS dispatch path,
introduce `task_executions` + `llm_calls`, wire coordinators to write the new tables, install
worker subprocess hooks, and delete the ~12 tables the old model required.

This is operator-team direct implementation (Alan + Bert + Carla direct PRs, no `treadmill plan submit`).

## Success criteria

1. `POST /api/v1/plans` no longer calls `dispatch_task`; submitted tasks stay in `registered`
   until a coordinator picks them up via WS.
2. `task_executions` and `llm_calls` tables exist and are written by the coordinator on task
   dispatch and subprocess exit.
3. `treadmill team up --repo <slug>` creates/updates `team_configs` with `evaluator_label` and
   `worker_labels`; installs worker `settings.json` with PostToolUse relay-inject hook.
4. Coordinator CLAUDE.md reflects the ADR-0087 lifecycle (task_executions writes, peer review
   assignment, CI/conflict loop, evaluator handoff).
5. `workflow_runs`, `workflow_run_steps`, `roles`, `skills`, `hooks`, `output_kind`,
   `task_validation`, DSPy corpora tables, `workflows`, `workflow_versions`,
   `workflow_version_steps` are all dropped. `starters.py` role seeding removed.
6. All existing non-deleted tests pass. New tests cover task_executions CRUD.

## Constraints / scope

### In scope
- All five ADR-0087 migration phases
- `task_status` VIEW updated to read `task_executions`
- CLI `treadmill team up` command
- Worker PostToolUse relay-inject hook template + deployment via `team up`
- Coordinator and evaluator CLAUDE.md updates (content only, not session wiring)

### Out of scope
- Live session bootstrapping (`treadmill team up` on running infra — operator step post-merge)
- Evaluator logic implementation (evaluator CLAUDE.md gets a stub; full judgment is follow-on)
- Peer review reviewer-selection logic (coordinator CLAUDE.md notes the pattern; routing-memory
  is accumulated organically)
- Dashboard / UI updates for task_executions
- SQS infrastructure teardown (operator step; queue can drain naturally)
- `redispatch.py` + `triggers.py` dispatch_task references beyond the plan-submit path (these
  serve the scheduler/synthetic-task path which has its own migration track)

### Budget
Three sessions (Alan + Bert + Carla) × until done. No artificial time cap; phases are the gate.

## Sequence of work

### Wave 1 — parallel (PR-A ∥ PR-B)

**PR-A — Alan: remove dispatch_task from plan-submit path**
Pre-work: grep `tests/` for `mock.*sqs|publish.*work_queue|dispatch_task` to identify all
test consumers before cutting the branch; flag the full surface so nothing goes unexpectedly red.

Add a `412 Precondition Failed` guard at `POST /api/v1/plans`: if no `team_configs` row exists
for `body.repo`, return `{"detail": "no team configured for repo — run: treadmill team up --repo <slug>"}`.
This ensures plan submit fails loudly rather than silently creating a plan with no coordinator
to pick it up.

Remove the `dispatch_task` calls in `routers/plans.py` (4 call sites) on the plan creation and
doc-merge paths. Tasks stay in `registered` after submit; coordinator picks them up via
`plan.submitted` WS event. Remove the `dispatch_task` call in `routers/tasks.py` line ~194
(manual retry path) — replace with a `task.registered` event emit so coordinator can re-pick-up.
Update `tests/test_integration_plans_router.py`, `tests/test_dispatch_unit.py`, and any
additional consumers surfaced by the pre-work grep to expect no SQS publish on plan submit.
Add a test for the 412 path (missing team_configs). AGENT.md for `routers/plans.py` component.

*Note: `redispatch.py` and `triggers.py` dispatch_task calls are out of scope for this PR —
they serve the scheduler synthetic-task path. Leave them wired; they become dead code after
Phase 4 and will be removed then.*

*Dev fast-path removal:* PR-A removes the `elif dev_active:` block entirely. The dev fast-path
was an autoscaler-era convenience (spawning immediate `wf-author` to bypass the PR-merge gate).
Under ADR-0087 all plans go through a coordinator; the fast-path's only function was to call
`dispatch_task`, which no longer exists. Remove: `dev_active` variable, `body.dev` guard logic,
the `elif dev_active:` block, and the `dev` field from `PlanCreateRequest` if it has no other
consumers. Local dev requires `treadmill team up --repo <slug>` once; then plan submit works
normally. Update AGENT.md + remove the `--dev` flag from `treadmill plan submit` CLI docs.

**PR-B — Bert: team_configs schema + `treadmill team up` CLI**
Alembic migration: `ALTER TABLE team_configs ADD COLUMN evaluator_label TEXT` only.
(`worker_labels TEXT[]` already exists from ADR-0085+0086 migration `20260609_1000_team_configs.py`;
do NOT re-add or the migration fails with "column already exists".) Update `models/team_config.py`
ORM + Pydantic schemas for `evaluator_label`. Update team_configs router to expose it.
Add `treadmill team up --repo <slug> [--workers N]` CLI command (superseding `treadmill repo add`
which becomes a deprecated alias) in `cli/treadmill_cli/commands/` that writes the team_configs
row with deterministic labels (`coordinator-<slug>`, `evaluator-<slug>`, `worker-<slug>-1…N`).
Creates `~/.treadmill/teams/<slug>/` directory tree per session:
- `~/.treadmill/teams/<slug>/<label>/` — one directory per session (coordinator + evaluator + workers)
- `~/.treadmill/teams/<slug>/<label>/.session-id` — empty file on creation; coordinator writes
  actual session ID on first subprocess exit; `--resume` reads from it on subsequent spawns
- `~/.treadmill/teams/<slug>/<label>/<label>.env` — env vars for the session unit
**Scale-down guard:** if `--workers N` reduces the worker count, check for in-flight work on
the to-be-removed labels. Guard implementation must handle two states:
- `task_executions` table exists (Phase 3+ state): query `WHERE worker_label IN (...) AND status='running'`; abort if any rows found.
- `task_executions` table does not yet exist (Phase 1-2 transition window): skip the check — old execution model is still wired; scale-down cannot orphan task_executions-tracked work.
Use `SELECT to_regclass('task_executions') IS NOT NULL` to test existence before querying.
Accept `--force` to skip the guard entirely (operator's explicit acknowledgment).
Enables + starts `treadmill-channel@<label>.service` units. Update `tests/test_routers_team_configs.py`.
CLI test (assert directory tree + .session-id stub files created + scale-down guard fires).
AGENT.md for team_configs component.

---

### Wave 2 — sequential (PR-C after PR-A merged)

**PR-C — Alan: task_executions + llm_calls tables + VIEW**
Alembic migration: CREATE `task_executions` + `llm_calls`. Schema per ADR-0087 §Schema changes:
- four-value trigger CHECK constraint (`initial`, `coordinator-rework`, `evaluator-rework`, `peer-review`)
- `failure_reason TEXT NULL` column for coordinator_restart and other failure annotations
- UNIQUE (task_id, trigger, worker_label, started_at) — prevents duplicate spawns on coordinator restart
ORM models. Update `task_status` VIEW to include `task_executions`-derived status alongside
existing workflow_runs-derived status (additive — both tables live during transition; prefer
task_executions when present). Add minimal CRUD endpoints:
`POST /api/v1/task_executions`, `PATCH /api/v1/task_executions/{id}`,
`GET /api/v1/task_executions?task_id=<id>`. Add `POST /api/v1/llm_calls`.
New test files: `tests/test_routers_task_executions.py` (task_executions CRUD) and
`tests/test_routers_llm_calls.py` (llm_calls POST + FK relationship). AGENT.md.

---

### Wave 3 — parallel (PR-D ∥ PR-E, both after PR-B + PR-C merged)

**PR-D — Bert: coordinator CLAUDE.md + evaluator_label wiring + failure recovery**
Pre-work: locate the coordinator CLAUDE.md template in the repo (likely under
`tools/coordinator/` or a session-templates directory — verify before coding; the live file
lives at `~/.treadmill/teams/<slug>/coordinator-<slug>/CLAUDE.md` and is installed by
`treadmill team up`). PR-D modifies the template, not the live file.
Update the coordinator's CLAUDE.md to reflect the ADR-0087 lifecycle:
- On `plan.submitted`: POST task_executions {trigger: initial}, cc-relay worker brief
- Monitor CI via `check_run.completed` events; POST task_executions {trigger: coordinator-rework} on failure
- Poll `task_mergeability` VIEW; POST task_executions {trigger: coordinator-rework} on conflict
- After CI green + clean: POST task_executions {trigger: peer-review} per reviewer, spawn reviewers
- After peer review collation: POST task_executions {trigger: coordinator-rework} if needed
- Brief evaluator via cc-relay; receive verdict; merge or POST task_executions {trigger: evaluator-rework}
- Write `task.ci_result`, `task.peer_review_verdict`, `task.evaluator_verdict` events via `POST /api/v1/events`

Also: ensure `team_configs` `evaluator_label` column is read by the coordinator's session-
bootstrap path (wherever the coordinator discovers its own repo config).

Add §Startup recovery section to coordinator CLAUDE.md:
- On start: query `task_executions WHERE status='running' AND started_at < NOW() - INTERVAL '1 hour'`; mark each `failed` with reason `coordinator_restart`
- On start: drain own relay inbox (`~/.cc-channels/coordinator-<slug>/relay/`) before processing any new WS events
- On start: re-poll `task_mergeability` VIEW for all open `task_prs` entries to catch state drift during the gap

Add `task.registered` handler to coordinator CLAUDE.md: coordinator subscribes to `task.registered`
events (in addition to `plan.submitted`). When a `task.registered` event arrives, coordinator
queries `task_dependency` for that task, checks dependency satisfaction, and dispatches if
unblocked. This covers manual-retry paths that emit `task.registered` instead of calling
`dispatch_task` (PR-A).

**PR-E — Carla: worker subprocess hook template + evaluator stub + team up deployment**
Pre-work: verify the exact JSON field name that `claude --print --output-format json` uses for
the session ID (likely `session_id` but confirm against the actual output before coding the
.session-id write logic).
Add worker settings.json template (PostToolUse hook on Bash: checks relay inbox, returns
`{"decision": "block", "reason": "[COORDINATOR]: <msg>"}` if message found). Add worker
CLAUDE.md template with: trust-coordinator-prompt declaration, cc-relay usage instructions,
task brief format. Add evaluator stub CLAUDE.md (identity, read-only API constraint, relay
verdict format — full judgment logic is follow-on). Update `treadmill team up` to install
worker settings.json + worker CLAUDE.md + evaluator stub CLAUDE.md into the respective session
directories (locating worker/evaluator session settings paths — consult `workers/agent/` layout).
Tests for hook script logic. AGENT.md for worker/evaluator template component.

---

### PR-H — wiring reconciliation (GATES the restart step; added 2026-06-10)

Discovered after Wave 3 merged: `install_team()` writes per-session config (CLAUDE.md,
settings.json) to `<team>/<label>/` subdirs, but `launch-session.sh` runs coordinators from the
team-dir cwd (and workers from the default workdir), and Claude Code discovers settings at
`<cwd>/.claude/settings.json`. The rendered files are never read. PR-H reconciles the layout:
launcher cwd → per-label subdir for every session; install_team() → `<label>/.claude/settings.json`;
remove the stale `<team>/CLAUDE.md`. Migration hazard: coordinator cwd change orphans the live
`--resume` transcript slug — handle in the same PR. Split between Bert (launcher) + Carla
(install.py). See `docs/learnings/2026-06-10-template-install-layout-vs-launcher-cwd-mismatch.md`.

### Between PR-H and Wave 4 — Alan restarts sessions (after PR-H merges)

After PR-H merges, **Alan runs `systemctl --user restart treadmill-channel@<label>.service`**
for each active coordinator and worker session so live sessions pick up the updated CLAUDE.md
(PR-D) and the PostToolUse hook settings.json (PR-E). Do NOT restart before PR-H —
without the wiring reconciliation the restart re-reads stale config (no-op). Also do NOT restart before both PRs merge —
a coordinator writing to `task_executions` with workers that lack the PostToolUse hook means
mid-execution steering is absent for any task dispatched in that window. The restart must
happen before PR-F drops `workflow_runs`.

### Wave 4 — sequential (PR-F → PR-G, after operator restart + PR-D merged)

**PR-F — Bert: delete old execution tables (Phase 4)**
Alembic migration includes precondition guard: acquire `LOCK TABLE workflow_runs IN EXCLUSIVE
MODE NOWAIT` before the SELECT check (fails immediately if an active coordinator holds a
transaction, preventing the SELECT-vs-INSERT race). Then check `SELECT MAX(created_at) FROM
workflow_runs`; if within last 5 min (configurable `DEPRECATED_TABLE_QUIESCE_SECONDS`, default
300), abort naming coordinator sessions to restart. Lock is released when the migration
transaction commits (or aborts). Fails loudly instead of silently dropping a table with an
active writer.
Drops: `workflow_runs`, `workflow_run_steps`, `roles`, `role_version`,
`role_skill`, `role_hook`, `skills`, `hooks`, `output_kind`, `task_validation`,
`architect_gold`, `validator_gold`, `review_dspy_variant_pr`, `triage_finding`,
`workflow_dispatch_dedup`. Remove ORM model files and any imports. Remove API router endpoints
that only served the dropped tables. Remove `dispatch.py` Dispatcher class and call sites
(by this point dispatch_task should have no live callers). Remove
`coordination/dispatch_dedup.py`. Update task_status VIEW to drop workflow_runs reference.
Remove tests for dropped components. AGENT.md cleanup.

**PR-G — Alan: delete workflow versioning + starters (Phase 5)**
Pre-work: audit `coordination/triggers.py` to understand what dispatch_task callers remain
after PR-F and what each one does — some may be dead code, others may need replacement with
a `task.registered` event or coordinator re-brief signal. Determine the correct replacement
before cutting the branch.
Alembic migration dropping: `workflows`, `workflow_versions`, `workflow_version_steps`.
Remove `seed/starters.py` role seeding from API startup (`app.py` lifespan). Remove
`coordination/triggers.py` synthetic-task dispatch_task path (replacement TBD from pre-work audit). Remove `coordination/redispatch.py` if fully dead.
Remove `coordination/cross_step.py` if fully dead. Clean up any remaining workflow_run
references in routers and tests. Final AGENT.md sweep.

---

## Risks / unknowns

- **task_status VIEW additive period (PR-C → PR-F):** During the gap between PR-C and PR-F,
  the VIEW reads both `workflow_runs` and `task_executions`. The VIEW must union cleanly;
  test coverage for mixed-state plans is needed.
- **dispatch_task remaining callers:** `triggers.py` and `redispatch.py` still call
  `dispatch_task` after PR-A. These serve the scheduler path which creates synthetic tasks.
  They become unreachable once the scheduler is retired (ADR-0087 §Health bots replaces this
  with coordinator-picked-up plans). Leave them until PR-F; they'll be deleted with the rest
  of the machinery.
- **SQS drain:** After PR-A, new plans no longer publish to SQS. Existing in-flight messages
  need a one-time drain (operator step: run the drain script or let them expire; queue TTL is
  the backstop).
- **Worker settings path:** `treadmill team up` needs to know where worker session settings
  live. PR-E author to verify against `workers/agent/` layout before coding.

## Decisions captured during execution

**2026-06-10: Wave 1–3 merged**

PRs merged in order: PR-A (#289), PR-B (#288), PR-C (#290), PR-E (#291). PR-D (Bert) in flight
(~80% done at 05:14Z, ETA 05:45Z). Session restarts gate on PR-D.

**2026-06-10: PR-G pre-work audit — triggers.py is full-delete**

`triggers.py` (3000+ lines) is entirely entangled with `WorkflowRun` / `WorkflowRunStep` /
`WorkflowVersion` — every function either creates these rows directly or queries them. No function
survives after PR-F drops those tables. Conclusion: delete the entire file in PR-G; no replacement
needed because the coordinator's CLAUDE.md (PR-D) explicitly covers all the cases triggers.py
handled (CI failure → coordinator-rework, conflict → coordinator-rework, evaluator rework,
operator escalation).

`redispatch.py`: queries `workflow_runs` directly in `_PENDING_TASKS_SQL`; also calls
`Dispatcher.dispatch_task`. Delete in PR-F alongside `dispatch_dedup.py`.

Sweep files (`auto_merge_loop.py`, `conflict_sweep.py`, `escalation_close_sweep.py`) are
independent of workflow_runs and should survive. `coordinator_overlay.py` (provides
`CapOverlayDecision`) is used only by triggers.py — delete with it in PR-G.

**2026-06-10: install.py coordinator placeholder — PR-D fills it**

Current `install.py:install_team()` has an explicit comment `# The coordinator's per-session
config is NOT written by this function — PR-D owns coordinator-side CLAUDE.md content.`
Bert is extending install.py in PR-D to render coordinator/CLAUDE.md.tmpl, folding in the
CLI-wiring follow-up Carla flagged.

**2026-06-10: Wave 3 complete (PR-D #292 merged) — but restart step GATED on a reconciliation PR**

All of PR-A/B/C/D/E merged. Before running the session-restart step, on-disk inspection of the
live `coordinator-medicoder` surfaced a 3-part layout seam between `install_team()` and
`launch-session.sh` + Claude Code's config discovery:

1. Launcher pins coordinator cwd to `<team>/`; `install_team()` renders coordinator CLAUDE.md to
   `<team>/coordinator-<slug>/` (subdir). Running coordinator reads the stale `<team>/CLAUDE.md`.
2. Worker `settings.json` rendered to `<team>/<label>/settings.json`, but Claude reads
   `<cwd>/.claude/settings.json` — PostToolUse hook never registers.
3. Launcher has per-label cwd handling only for `coordinator-*`; workers/evaluator run from
   default workdir.

Restarting now would re-read stale config — no-op. **The restart step is blocked on a wiring
PR** that reconciles install_team() output with launcher cwd + Claude `.claude/` discovery.
Leading option: per-label-subdir cwd for every session + `<label>/.claude/settings.json` +
remove stale `<team>/CLAUDE.md`. HAZARD: coordinator cwd change orphans the live `--resume`
transcript slug — needs migration. Captured in
`docs/learnings/2026-06-10-template-install-layout-vs-launcher-cwd-mismatch.md`. Relayed to
Bert (launcher/CLI) + Carla (install.py) to split + own. This becomes **PR-H (wiring)**, inserted
before the restart step and Wave 4.

**2026-06-10: PR-H merged (#293), PR-I merged (#294) — restart executed; medicoder team live on canonical slug**

PR-I (Alan, sibling co-signs from Bert + Carla) closed the last wiring gap: `team up` now calls
`install_team()`. Gate verification passed: scratch-slug install (10/10 checks incl. stale-root
unlink + `.claude/settings.json`), real-launcher end-to-end with fake claude (per-label cwd +
`TREADMILL_SESSION_LABEL` + per-label env all correct).

Two operational surprises during the restart:

1. **Slug derivation cutover.** `treadmill team up MediCoderHQ/medicoder` derives slug
   `medicoderhq-medicoder`, not the hand-rolled ADR-0084-era `medicoder`. The upsert rewrote
   `team_configs` to the canonical labels and `team up` started units for them. Decision: adopt
   the canonical derivation (it is the merged design — "no manual override"); the old
   `coordinator-medicoder` session was retired (its transcript was being reset anyway per
   option 3), `memory.md` copied into the new coordinator's cwd, old `~/.treadmill/teams/medicoder/`
   tree left in place for later cleanup. The live team is now `coordinator-medicoderhq-medicoder`,
   `evaluator-medicoderhq-medicoder`, `worker-medicoderhq-medicoder-1/2`.
2. **settings.json `$comment` wedge.** Claude Code's boot-time settings validation flags the
   template's `$comment` keys (non-schema) with an interactive "values were skipped — Continue?"
   prompt; both workers wedged on first boot. Fixed operationally (clean settings applied on disk +
   unit restart) and durably via PR #295 (strip `$comment` keys; constraint pinned in AGENT.md).

Also fixed en route: local dev API was down (image predated the PR-B migration the DB was stamped
with — rebuilt from main, `treadmill-local up --deployment personal`); `treadmill-local` editable
install pointed at Carla's worktree (repointed to the main repo).

Verified post-restart: coordinator booted on the new ADR-0087 template (cites §2 startup
checklist), evaluator clean, all sessions at per-label cwd. **PR-F is unblocked.**

**2026-06-10: Wave 4 complete — PR-F (#297), PR-G (#298), hotfix (#299)**

PR-F merged with two review fixes (B1: pr_merged clause precedence prevents re-dispatch of
pre-ADR merged tasks; B2: boot-time starters auto-seed removed before its roles-table gate could
crash-loop the post-migration boot). PR-G merged with both sibling co-signs: final four tables +
`tasks.workflow_version_id` dropped, trigger evaluator + sweep family + workflow models deleted,
dispatch.py reduced to the durable-event seam with the `DispatchPublishFailed` marker write moved
into `persist_and_publish` (keeps the ReplayLoop genuinely load-bearing). Four deploy residuals followed,
all the same class — surfaces that exist only at runtime, invisible to import-graph analysis and
dispatcher-stubbed unit tests: (#299) the mergeability VIEW rewrite narrowed its column
projection, breaking two consumers' SELECTs; (#299) `POST /api/v1/events` passed dict payloads
into `model_dump` — broken since the route shipped, masked by stubbed tests; (#300) `Event.run_id`
/`.step_id` still declared string-based ORM FKs to dropped tables — SQLAlchemy resolves them at
flush, so every events INSERT 500'd; (#300) the dashboard accounts token rollup still joined
`workflow_run_steps`. Detection guards added: a metadata-wide FK-resolution test
(`sorted_tables`), a real-dispatcher events regression test, and a service-wide live-SQL grep
(clean — the set is complete).

Incident during the window: an uncommitted PR-G migration leaked into a dev image build
(untracked files survive branch switches; docker copies the worktree) and ran against the dev DB,
putting the schema ahead of deployed code — task-state endpoints 500'd until PR-G deployed; the
live coordinator escalated the outage and was answered with the resolution path. Memory captured:
never docker-build from a dirty worktree.

## Post-mortem

- **What worked.** The wave structure with explicit KEPT-until-PR-G import-graph notes made an
  ~25,000-line deletion reviewable in two PRs. Sibling co-sign caught real bugs at every step:
  Bert's PR-C P0 (IntegrityError→409), Alan's PR-F B1/B2, Carla's PR-E trust-boundary tests.
  The pre-restart on-disk audit (instead of trusting "templates merged") caught the PR-H layout
  seam that would have made the restart a silent no-op.
- **What surprised us.** Five integration seams that no unit test could see: (1) install-path vs
  launcher-cwd vs Claude Code discovery (PR-H); (2) `$comment` keys wedging unattended boots
  (#295); (3) killed pre-transcript sessions poisoning `--resume` into systemd crash-loops;
  (4) the team-up slug derivation rewriting a hand-rolled team's identity; (5) the mergeability
  VIEW's column contract (#299). All five share one shape: two halves built correctly against a
  prose spec that never pinned the exact runtime contract.
- **What should become an ADR, learning, or rule.** Learnings written:
  template-install-layout-vs-launcher-cwd-mismatch, killed-prelaunch-session-orphans-resume-loop;
  memories: no-docker-build-dirty-worktree, no-$comment-keys-in-unattended-settings,
  cc-relay-use-file-for-backticks. Candidate rule: any artifact a runtime loads by convention
  (CLAUDE.md, settings.json, VIEW columns, env files) gets one test that asserts the consumer's
  exact contract, not the producer's output shape.
- **Open follow-ups.** (1) Skip-gated integration tests still seed dropped tables for surviving
  surfaces — rewrite against task_executions seeding as part of the e2e push. (2) Launcher should
  validate a transcript exists before `--resume` (crashloop class). (3) Schedules are inert
  (scheduler publishes ticks nobody consumes) pending ADR-0087 §Health bots. (4) `auto_merge`
  plan frontmatter is inert — the coordinator merges on evaluator-approve; plan-skill doc should
  drop the flag. (5) The wf-* workflow vocabulary in plan docs is ignored — plan-skill template
  update.
- **What this plan teaches us about future plans.** Operator-team direct implementation with
  per-PR sibling co-sign sustained ~9 merged PRs in one session without operator intervention —
  the consensus-merge model works. The costliest moments were all integration seams between
  separately-authored halves; the cheapest fix was always one session doing an on-disk audit
  before declaring a phase done.
