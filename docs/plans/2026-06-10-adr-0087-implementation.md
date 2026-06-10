# Plan: ADR-0087 implementation — long-lived team execution model

- **Status:** active
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

### Between Wave 3 and Wave 4 — Alan restarts sessions (after BOTH PR-D AND PR-E merge)

After both PR-D and PR-E merge, **Alan runs `systemctl --user restart treadmill-channel@<label>.service`**
for each active coordinator and worker session so live sessions pick up the updated CLAUDE.md
(PR-D) and the PostToolUse hook settings.json (PR-E). Do NOT restart before both PRs merge —
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

## Post-mortem

*(filled when completed or abandoned)*
