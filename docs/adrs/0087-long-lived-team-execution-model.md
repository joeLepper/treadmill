# ADR-0087 — Long-lived team execution model

- **Status:** proposed
- **Date:** 2026-06-10
- **Supersedes:** ADR-0018, ADR-0022, ADR-0029, ADR-0032, ADR-0084, ADR-0086 (substantially)
- **Authors:** treadmill-alan, treadmill-bert

## Context

Treadmill was originally built around ephemeral Docker workers (ADR-0018): an autoscaler
polled an SQS queue, spawned containers for each work item, and the container processed one
step before exiting. Each container was stateless; the database was the coordination layer.

ADR-0084 and ADR-0086 introduced coordinators as long-lived named sessions that own lifecycle
bookkeeping. That was the first step toward the model described here. However, those ADRs
retained the old dispatch machinery (SQS, `dispatch_task`, workflow version steps) and did
not fully specify the worker or evaluator roles.

On 2026-06-10, a gap audit (Alan + Bert) found three writer-conflicts, a missing consumer
(40 SQS messages with no subscribers), and a fundamental mismatch between the coordinator
model and the still-running autoscaler machinery. Joe directed a full rethink.

## Decision

Replace the ephemeral Docker worker model entirely with a **long-lived named-session team**
per repo. All repos are coordinator-owned. No autoscaler. No SQS **worker dispatch** queue.

The `events` table is the durable event log for all state transitions including `plan.submitted`.
The WS subscription is the real-time delivery mechanism for online coordinators. On reconnect,
a coordinator replays missed events by querying the `events` table for unprocessed
`plan.submitted` entries (tasks with no `task_executions` row). This preserves the same
ordering guarantee as the existing GitHub webhook intake path.

### The three-locus model

```
API / DB              Coordinator              Evaluator
─────────────         ────────────────         ─────────────────
Event log +           Live coordination        Independent audit
state store           engine. Routes work,     node. Evaluates PRs,
(durable queue)       writes lifecycle         returns approve or
                      state to DB.             rework to coordinator
Records what          Receives plan.submitted         │
happened. WS          via WS (real-time) or           │
delivers in           events-table replay             │
real-time;            on reconnect.                   │
events table                  │                       │
is the durable                │ cc-relay              │ cc-relay
log.                          ▼                       │
                         Workers                      │
                         ──────────────               │
                         Long-lived named             │
                         implementation               │
                         sessions. One team    ◄──────┘
                         per repo.
```

### Per-repo team shape

Every registered repo gets a software team:

| Role | Label pattern | Count | Responsibility |
|---|---|---|---|
| Coordinator | `coordinator-<slug>` | 1 | Routes tasks to workers, owns lifecycle writes |
| Evaluator | `evaluator-<slug>` | 1 | Evaluates PRs independently; verdicts merge or rework |
| Workers | `worker-<slug>-N` | configurable | Implement code, open PRs |

`team_configs` stores `coordinator_label` (string), `evaluator_label` (string),
and `worker_labels` (string array). Worker count is configurable per repo; default 3.

Worker labels are derived deterministically from the repo slug at bootstrap time
(`worker-<slug>-1`, `worker-<slug>-2`, `worker-<slug>-3`). No manual assignment required.

Orchestrators (`treadmill-alan`, `treadmill-bert`, `treadmill-carla`, `treadmill-donna`)
are executives who submit plans. They are never in `worker_labels`.

### Task execution flow

```
1. Orchestrator submits plan
   POST /api/v1/plans → persists plan + tasks → emits plan.submitted
   (no dispatch_task; no SQS send)

2. Coordinator receives plan.submitted via WS (?coordinator_label=... filter) or events-table
   replay on reconnect (queries for plan.submitted events with no task_executions rows).
   Reads tasks. For each unblocked task:
   - POST /api/v1/task_executions {task_id, worker_label, trigger="initial"}
   - Briefs worker via cc-relay

3. Worker executes task
   Writes code, opens PR.
   Reports "PR: #N" to coordinator via cc-relay.

4. Coordinator registers PR
   POST /api/v1/task_prs {task_id, repo, pr_number}
   Briefs evaluator via cc-relay: "PR #N is ready for evaluation"
   (The evaluator knows work is evaluable because the coordinator says so. The coordinator
   is the only session that knows a PR has been opened and registered.)

5. Evaluator evaluates
   GET /api/v1/task_executions/{task_id} — reads current state (read-only)
   Checks CI, branch state, rules distilled from learnings, repo memories.
   Verdict via cc-relay to coordinator (fixed format):

     [from: evaluator-<slug>]
     [verdict: approve | rework]
     [pr_number: N]
     [task_id: <uuid>]

     <one paragraph summary of verdict reasoning>

     <for rework: bulleted remediation list — coordinator pastes verbatim into worker's next brief>

   On APPROVE → coordinator merges PR, PATCH task_execution {status: completed}
   On REWORK  → coordinator POST task_executions {trigger: "evaluator-rework"}, re-briefs worker

   Coordinator writes a task.evaluator_verdict event on receipt of either verdict. Audit trail.

6. On coordinator-initiated rework (CI failure, worker error):
   POST task_executions {trigger: "coordinator-rework"}, re-briefs worker.

7. On task complete (PR merged):
   Coordinator PATCH task_execution {status: completed, completed_at, token_usage (see §Token)}
   Emits pr_merged event path per ADR-0086 §12.4.
```

### Evaluator WS subscription

The evaluator subscribes with `?evaluator_label=evaluator-<slug>`. This filter composes with
the existing `coordinator_label`, `created_by`, and `plan_ids` filters by OR
(per the WS filter implementation in PR #286). Evaluator receives events for its repo's plans.

Evaluator is **read-only API**. All lifecycle writes flow through the coordinator.

*Why relay-based (evaluator → coordinator → DB) instead of direct evaluator writes?* Two
reasons. First, **single-writer invariant**: coordinator is the only mutator of lifecycle
state; no multi-writer races on `task_executions`. Second, **judgment carries in-band**: the
evaluator's relay carries verdict reasoning + remediation list directly to the coordinator,
which incorporates it verbatim into the worker's next brief. Direct API writes would force
the coordinator to re-fetch and re-derive the evaluator's intent. The relay format carries
meaning in-band; schema would carry only IDs.

### Health bots

Health bots become periodically-dispatched plans. Scheduler cron fires →
`POST /api/v1/plans` (created_by=scheduler, task intent encoded) →
`plan.submitted` emitted → coordinator picks up via WS subscription as any other plan.
Schedules table preserved; no workflow_version lookup required.

### Team bootstrap

```bash
treadmill team up --repo MediCoderHQ/medicoder
```

Writes `team_configs` row; spawns systemd units for each named session. Worker labels and the
evaluator label are derived deterministically from the repo slug — no manual naming required:

```
coordinator-medicoder
evaluator-medicoder
worker-medicoder-1
worker-medicoder-2
worker-medicoder-3   (default count: 3; override with --workers N)
```

Sessions are created fresh if they don't exist; the command is idempotent (re-running against
an existing team config updates worker count without replacing running sessions).

### Token economics

**[DEFERRED — pending empirical decision]**

Two options under evaluation:

**Option A — Subprocess-per-task (preserves clean attribution):**
Workers dispatch each task as a `claude --print --output-format json` subprocess internally.
Tokens are extracted at subprocess exit and tagged with `task_id`. The worker session process
is long-lived (preserves repo memory, CLAUDE.md context, cc-relay channel connection); only
the per-task LLM invocation is subprocess-scoped. The outer session continues to receive
cc-relay messages from coordinator and peer workers between (and alongside) task subprocesses —
two-way channel communication is unaffected by the subprocess model.

**Option B — Aggregate tracking:**
Workers run in fully interactive mode. Token attribution is per-session via Anthropic API
usage reports. Per-task breakdown is unavailable; per-plan totals are approximated.

Claude Code v2.1.170 only reports token usage at subprocess exit. No mid-session polling
exists. Decision gates on which worker invocation model is chosen.

**Token columns on `task_executions` are withheld from the schema until this is resolved.**

**If Option A:** add `llm_calls` table FK to `task_executions`:

```sql
CREATE TABLE llm_calls (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_execution_id   UUID NOT NULL REFERENCES task_executions(id) ON DELETE CASCADE,
    input_tokens        BIGINT NOT NULL,
    output_tokens       BIGINT NOT NULL,
    cache_creation_tokens BIGINT,
    cache_read_tokens   BIGINT,
    model               TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_llm_calls_task_execution_id ON llm_calls (task_execution_id);
```

One row per Claude Code subprocess invocation. Many-to-one against `task_executions` because
a single task may dispatch multiple subprocesses (initial code-write + CI-failure handling
mid-task). Per-plan burn aggregates through the `tasks → task_executions → llm_calls` JOIN chain.

**If Option B:** no `llm_calls` table. Per-task attribution is lost. Per-plan totals are
approximated via date-range correlation against Anthropic API usage reports.

### Rework tracking

Each re-brief is a new `task_executions` row, not a counter increment. This preserves
per-cycle token attribution and answers the key question: "did cycle 2 burn more tokens
because the worker needed more context?"

Rework count per task = `COUNT(*) WHERE task_id = X AND trigger != 'initial'`
Per-plan rework = `SUM(rework counts) GROUP BY plan_id`

The trigger taxonomy:
- `initial` — first brief on a task
- `coordinator-rework` — coordinator re-brief (CI failure, worker error, dependency unblocked)
- `evaluator-rework` — evaluator requested changes

This is the primary metric for whether long-lived context-sharing reduces loops.

## Schema changes

### Add

```sql
-- Replaces workflow_runs + workflow_run_steps
CREATE TABLE task_executions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id     UUID NOT NULL REFERENCES tasks(id),
    worker_label TEXT NOT NULL,
    trigger     TEXT NOT NULL CHECK (trigger IN ('initial','coordinator-rework','evaluator-rework')),
    status      TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','completed','failed')),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    -- token columns added when Option A/B decision is made
    UNIQUE (task_id, started_at)  -- dedup guard
);

-- team_configs gains evaluator_label
ALTER TABLE team_configs ADD COLUMN evaluator_label TEXT;
```

### Keep (unchanged)
`plans`, `tasks`, `task_dependency`, `task_prs`, `task_board`, `events`, `team_configs`,
`escalations`, `schedules`, `repo_configs`, `task_status` VIEW, `task_mergeability` VIEW.

### Delete
- `workflow_runs`, `workflow_run_steps`
- `workflows`, `workflow_versions`, `workflow_version_steps`
- `workflow_trigger`, `workflow_dispatch_dedup`
- `roles`, `role_version`, `role_skill`, `role_hook`, `skills`, `hooks`, `output_kind`
- `task_validation`
- `architect_gold`, `validator_gold`, `review_dspy_variant_pr`, `triage_finding`
  (DSPy corpora — task-tailored briefs replace standardized prompt tuning)
- `dispatch_task()` function and all five call sites
- SQS work queue send path in `dispatch.py`

## Migration phases

**Phase 1** (no-risk, same-day): Delete `dispatch_task()` call from `plans.py` plan-submit
path. Tasks created at submit stay in `registered` status until coordinator §12.1 handler
fires. One-time SQS drain for orphaned messages.

**Phase 2** (same-day): Add `evaluator_label` to `team_configs`. Populate `worker_labels`
with actual worker session labels per repo. Run `treadmill team up` bootstrap for each
active repo.

**Phase 3** (schema migration): Create `task_executions`. Migrate `task_status` VIEW to
read it. Update coordinator §12.2 path to write `task_executions` instead of `workflow_runs`.
Token columns added once Option A/B is decided.

**Phase 4**: Delete `workflow_runs`, `workflow_run_steps`, roles/skills/hooks machinery,
task_validation, DSPy corpora tables. Alembic migration.

**Phase 5**: Delete `workflows`, `workflow_versions`, `workflow_version_steps`. Alembic.
Remove starters.py role-seeding on API startup.

## Consequences

**Positive:**
- Single clear dispatch path: plan.submitted → coordinator → worker. No competing paths.
- Rework measurement is native to the schema, not inferred from event sequences.
- Evaluator as independent auditor decouples quality judgment from implementation momentum.
- Long-lived sessions accumulate repo-specific memory, reducing context re-establishment cost.
- ~12 tables deleted; API surface shrinks; no autoscaler operational burden.

**Negative / risks:**
- Token attribution model is not yet decided. If Option B, per-task granularity is lost.
- Three named sessions per repo (coordinator + evaluator + N workers) increases operational
  surface for session restarts and context recovery.
- No UI plan for the new model. Task tracking surfaces need a separate pass.
- DSPy prompt optimization corpus is lost. If future work needs it, corpora must be
  rebuilt from scratch.

## Open questions

1. **Token path (A vs B):** Joe's call pending. Determines `task_executions` token columns.
2. **Worker specialization:** ~~Generalist for now~~ — **CLOSED** (2026-06-10). Generalist is
   the permanent default. Specializations are expected to emerge naturally through task
   routing history (orchestrators tend to route to workers who last knew the area best).
   No `worker_capabilities` column needed in v1.
3. **Health-bot brief shape:** Deferred. Coordinator's memory tells it what to do when
   it receives a health-check plan.
4. **UI:** Subsequent pass. May be task-tracking only or eliminated.
5. **Cross-repo team-tier sharing:** ~~Deferred~~ — **CLOSED** (2026-06-10). Workers are
   dedicated to their repo. OS processes are cheap. No cross-repo worker sharing.

## Decisions captured during execution

- 2026-06-10: Token mid-session reporting confirmed absent from Claude Code v2.1.170.
  Schema token columns withheld pending path decision. See §Token economics.
- 2026-06-10: **Empirical-first on token path.** We do not design the token attribution model
  in the abstract; we observe the first few plan runs under the new execution model and let
  actual data drive Option A vs B. Schema extension is intentionally deferred so we do not
  over-engineer a path that may not reflect real worker invocation patterns.
- 2026-06-10: **Single-writer invariant confirmed as architectural principle.** Coordinator is
  the sole writer of `task_executions` rows and lifecycle state. Evaluator reads only. Workers
  report outcomes via relay; coordinator transcribes into DB. No exceptions — multiple writers
  on lifecycle state produce races that are difficult to diagnose and replay.
- 2026-06-10: **Subprocess-per-task framing preferred if feasible.** The existing
  `claude_code.py` already emits `--output-format json` at subprocess exit and parses token
  counts per step (line 432). Option A is the structurally cleaner path; token attribution
  is clean-by-construction rather than inferred. If worker invocation model allows it, ship
  Option A. Confirmed this framing before deferring the schema extension.
