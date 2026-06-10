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

### Worker execution model

**DECIDED (2026-06-10): subprocess-per-task with named session continuity.**

Experimentally verified on Claude Code v2.1.170. Workers are not persistent interactive
sessions — they are `--resume`-based subprocess chains: each task dispatch is a fresh
`claude --print` invocation that resumes the same named session ID. The session accumulates
conversation history and repo-specific CLAUDE.md context across tasks; the process exits and
is re-spawned per task. This is the long-lived-team model: persistent *memory*, not
persistent *process*.

**Dispatch:**
```
claude --print \
  --resume <worker-session-id> \
  --output-format json \
  --permission-mode bypassPermissions \
  --settings <worker-settings.json> \
  "<task brief + any pre-drained relay messages>"
```

**Two-way relay — both directions experimentally proven:**

*Worker → Coordinator:* subprocess uses Bash tool to call `cc-relay.py`. Message lands in
coordinator's relay inbox via treadmill-events MCP. Works in `--print` mode with
`bypassPermissions`.

*Coordinator → Worker (mid-execution):* worker's `settings.json` configures a `PostToolUse`
hook on Bash. After each Bash tool call, the hook checks the worker's relay inbox. If a
message is present, hook returns `{"decision": "block", "reason": "[COORDINATOR]: <msg>"}`.
Claude Code injects this into Claude's context as a hook message; the worker sees it on the
next LLM turn and can change course. Injection is at **tool-use boundaries**, not
mid-LLM-turn — this is sufficient for all coordinator steering scenarios.

*Coordinator → Worker (on re-spawn):* when a subprocess exits, the coordinator checks the
worker's relay inbox. If messages are waiting (sent while the subprocess was running or after
it exited), the coordinator pre-drains them and includes them in the next `--resume` prompt.
The worker's CLAUDE.md declares coordinator-spawned prompts as trusted so it does not flag
pre-drained relay content as injection.

```
sequenceDiagram
    participant ORCH as orchestrator
    participant API as API / events table
    participant COORD as coordinator-<slug>
    participant FS as relay inbox (filesystem)
    participant W as worker-<slug>-N subprocess
    participant EVAL as evaluator-<slug>

    ORCH->>API: POST /plans → emits plan.submitted
    API-->>COORD: WS push (or events-table drain on reconnect)
    COORD->>API: POST /task_executions {trigger: initial}
    COORD->>FS: pre-drain worker inbox (append to brief)
    COORD->>W: spawn claude --print --resume <id> "task brief"

    loop each Bash tool call
        W->>W: executes tool
        Note over W,FS: PostToolUse hook fires
        FS-->>W: inject pending relay (decision:block+reason) if any
    end

    alt coordinator has mid-task steering message
        COORD->>FS: write relay to worker inbox
        Note over W,FS: picked up at next PostToolUse boundary
    end

    W->>FS: cc-relay "PR: #N opened"
    COORD-->>COORD: receives via treadmill-events MCP
    W->>W: subprocess exits; token JSON emitted

    COORD->>FS: check worker inbox post-exit
    alt pending messages in inbox
        COORD->>W: re-spawn --resume <id> with drained inbox in prompt
    else inbox empty
        COORD->>API: POST /task_prs {task_id, pr_number}
        COORD->>EVAL: relay "PR #N ready for evaluation"
    end

    EVAL->>API: GET /task_executions (read-only)
    EVAL->>ORCH: relay verdict [approve|rework]
    COORD-->>COORD: receives verdict via treadmill-events
    alt approve
        COORD->>API: PATCH task_execution {status: completed}
    else rework
        COORD->>API: POST /task_executions {trigger: evaluator-rework}
        COORD->>W: re-spawn with rework brief
    end
```

### Token economics

**DECIDED (2026-06-10): Option A — subprocess-per-task.**

Per-task token counts are captured from `--output-format json` at each subprocess exit.
`llm_calls` table records one row per subprocess invocation, FK to `task_executions`.
Multiple subprocesses per task execution (initial write + CI fix + rework) are all captured.

Add `llm_calls` table FK to `task_executions`:

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

**Phase 3** (schema migration): Create `task_executions` + `llm_calls`. Migrate `task_status`
VIEW to read `task_executions`. Update coordinator §12.2 path to write `task_executions`.
Install worker `settings.json` with PostToolUse relay-inject hook + `bypassPermissions`.

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
- Worker subprocess exits after each task — coordinator must manage re-spawn lifecycle.
  Session ID persistence and inbox drain logic add coordinator implementation complexity.
- Three named sessions per repo (coordinator + evaluator + N workers) increases operational
  surface for session restarts and context recovery.
- No UI plan for the new model. Task tracking surfaces need a separate pass.
- DSPy prompt optimization corpus is lost. If future work needs it, corpora must be
  rebuilt from scratch.

## Open questions

1. **Token path:** ~~Pending~~ — **CLOSED (2026-06-10).** Option A (subprocess-per-task).
   Experimentally verified. `llm_calls` table added to schema.
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
- 2026-06-10: **Option A decided and experimentally verified.** Subprocess-per-task with
  `--resume <session-id>` gives both per-task token attribution (via `--output-format json`)
  and accumulated session context across tasks. Two-way relay works: worker→coordinator via
  Bash tool; coordinator→worker mid-execution via PostToolUse hook `decision:block+reason`;
  coordinator→worker on re-spawn via pre-drained inbox in prompt. `UserPromptSubmit`
  additionalContext and Stop hook `continue:true` are both ignored in `--print` mode —
  neither is needed given the above paths.
