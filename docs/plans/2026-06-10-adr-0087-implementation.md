# Plan: ADR-0087 implementation — long-lived team execution model

- **Status:** drafting
- **Date:** 2026-06-10
- **Related ADRs:** ADR-0087
- **Authors:** treadmill-alan, treadmill-bert

## Goal

Ship the execution model described in ADR-0087: kill the autoscaler / SQS dispatch path,
introduce `task_executions` + `llm_calls`, wire coordinators to write the new tables, install
worker subprocess hooks, and delete the ~12 tables the old model required.

This is operator-team direct implementation (Alan + Bert direct PRs, no `treadmill plan submit`).

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
Two sessions × until done. No artificial time cap; phases are the gate.

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

**PR-B — Bert: team_configs schema + `treadmill team up` CLI**
Alembic migration: `ALTER TABLE team_configs ADD COLUMN evaluator_label TEXT; ALTER TABLE
team_configs ADD COLUMN worker_labels TEXT[]` (both columns — evaluator_label for single
evaluator session label, worker_labels for the full array of worker session labels). Update
`models/team_config.py` ORM + Pydantic schemas. Update team_configs router to expose new fields.
Add `treadmill team up --repo <slug> [--workers N]` CLI command (superseding `treadmill repo add`
which becomes a deprecated alias) in `cli/treadmill_cli/commands/` that writes the team_configs
row with deterministic labels (`coordinator-<slug>`, `evaluator-<slug>`, `worker-<slug>-1…N`).
Creates `~/.treadmill/teams/<slug>/` directory tree per session:
- `~/.treadmill/teams/<slug>/<label>/` — one directory per session (coordinator + evaluator + workers)
- `~/.treadmill/teams/<slug>/<label>/.session-id` — empty file on creation; coordinator writes
  actual session ID on first subprocess exit; `--resume` reads from it on subsequent spawns
- `~/.treadmill/teams/<slug>/<label>/<label>.env` — env vars for the session unit
Enables + starts `treadmill-channel@<label>.service` units. Update `tests/test_routers_team_configs.py`.
CLI test (assert directory tree + .session-id stub files created). AGENT.md for team_configs component.

---

### Wave 2 — sequential (PR-C after PR-A merged)

**PR-C — Alan: task_executions + llm_calls tables + VIEW**
Alembic migration: CREATE `task_executions` + `llm_calls` (full schema per ADR-0087 §Schema
changes — four-value CHECK constraint including `peer-review`). ORM models. Update `task_status`
VIEW to include `task_executions`-derived status alongside existing workflow_runs-derived status
(additive — both tables live during transition). Add minimal CRUD endpoints:
`POST /api/v1/task_executions`, `PATCH /api/v1/task_executions/{id}`,
`GET /api/v1/task_executions?task_id=<id>`. Add `POST /api/v1/llm_calls`.
New test file `tests/test_routers_task_executions.py`. AGENT.md.

---

### Wave 3 — parallel (PR-D ∥ PR-E, both after PR-B + PR-C merged)

**PR-D — Bert: coordinator CLAUDE.md + evaluator_label wiring + failure recovery**
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

**PR-E — Alan: worker subprocess hook template + evaluator stub + team up deployment**
Add worker settings.json template (PostToolUse hook on Bash: checks relay inbox, returns
`{"decision": "block", "reason": "[COORDINATOR]: <msg>"}` if message found). Add worker
CLAUDE.md template with: trust-coordinator-prompt declaration, cc-relay usage instructions,
task brief format. Add evaluator stub CLAUDE.md (identity, read-only API constraint, relay
verdict format — full judgment logic is follow-on). Update `treadmill team up` to install
worker settings.json + worker CLAUDE.md + evaluator stub CLAUDE.md into the respective session
directories (locating worker/evaluator session settings paths — consult `workers/agent/` layout).
Tests for hook script logic. AGENT.md for worker/evaluator template component.

---

### Operator step — after PR-D merges (before PR-F)

After PR-D merges, the coordinator CLAUDE.md instructs the session to write `task_executions`
instead of `workflow_runs`. **Joe restarts each active coordinator session** (`treadmill team up`
re-invoked, or `systemctl --user restart treadmill-channel@coordinator-<slug>.service`) so the
live session picks up the updated CLAUDE.md. This must happen before PR-F drops `workflow_runs`
— a live coordinator writing to a dropped table would lose work.

### Wave 4 — sequential (PR-F → PR-G, after operator restart + PR-D merged)

**PR-F — Bert: delete old execution tables (Phase 4)**
Alembic migration dropping: `workflow_runs`, `workflow_run_steps`, `roles`, `role_version`,
`role_skill`, `role_hook`, `skills`, `hooks`, `output_kind`, `task_validation`,
`architect_gold`, `validator_gold`, `review_dspy_variant_pr`, `triage_finding`,
`workflow_dispatch_dedup`. Remove ORM model files and any imports. Remove API router endpoints
that only served the dropped tables. Remove `dispatch.py` Dispatcher class and call sites
(by this point dispatch_task should have no live callers). Remove
`coordination/dispatch_dedup.py`. Update task_status VIEW to drop workflow_runs reference.
Remove tests for dropped components. AGENT.md cleanup.

**PR-G — Alan: delete workflow versioning + starters (Phase 5)**
Alembic migration dropping: `workflows`, `workflow_versions`, `workflow_version_steps`.
Remove `seed/starters.py` role seeding from API startup (`app.py` lifespan). Remove
`coordination/triggers.py` synthetic-task dispatch_task path (replace with a no-op or
task.registered event). Remove `coordination/redispatch.py` if fully dead.
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

*(populated as we work)*

## Post-mortem

*(filled when completed or abandoned)*
