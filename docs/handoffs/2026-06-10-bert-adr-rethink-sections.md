# bert's sections for the post-rethink ADR

Drop-in markdown for Alan's joint memo / ADR draft. Three sections: metrics, architect-as-auditor, open questions. Assumes Path A on token tracking (subprocess-per-task) with Path B contingency flagged.

---

## Metrics — token attribution + rework counting

Two metrics are non-negotiable per Joe: **per-task + per-plan token burn**, and **rework count** (how many times a task cycled). The architectural rethink is meant to reduce the second by sharing context in long-lived teams; we need the metric in place to measure the reduction.

### Token attribution

Claude Code reports token usage **only** at subprocess exit via `--output-format json`. Long-lived interactive sessions have no mid-session query mechanism; no accumulated stats file persists between tasks. The clean per-container attribution the old Docker workers had came from `--print` mode (subprocess-per-task).

**Adopt Path A — subprocess-per-task for LLM invocation.** A worker session is long-lived for **memory + repo context**; each task is dispatched internally as a `claude --print --output-format json` subprocess. The session as a process persists; the per-task LLM call is subprocess-scoped. Token usage is extracted from the subprocess's JSON output and reported with `task_id` tag.

This is structurally compatible with the long-lived-team hypothesis: the long-lived aspect is repo-specific memory + accumulated CLAUDE.md context + worker-side rules, **not** a single continuous LLM context window across tasks. Each task gets a fresh context window; the worker session keeps the meta-context that informs how to brief that window.

**Implementation:**

* Worker dispatches each task as a `claude --print --output-format json --append-system-prompt <worker-CLAUDE.md>` subprocess (or equivalent shape).
* Parses the JSON output at subprocess exit; extracts `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `model`.
* Reports per-subprocess attribution to the API via a new `llm_calls` row write at subprocess close.

**Schema:**

```sql
CREATE TABLE llm_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_execution_id UUID NOT NULL REFERENCES task_executions(id) ON DELETE CASCADE,
    input_tokens BIGINT NOT NULL,
    output_tokens BIGINT NOT NULL,
    cache_creation_tokens BIGINT,
    cache_read_tokens BIGINT,
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_llm_calls_task_execution_id ON llm_calls (task_execution_id);
```

One row per Claude Code subprocess invocation. Many-to-one against `task_executions` because a single task may dispatch the worker's subprocess multiple times (e.g., one for the initial code-write, another for handling a CI failure mid-task).

**Aggregations:**

* Per-task burn: `SELECT SUM(input_tokens + output_tokens) FROM llm_calls WHERE task_execution_id = X`
* Per-plan burn: `SELECT SUM(...) FROM llm_calls lc JOIN task_executions te ON te.id = lc.task_execution_id JOIN tasks t ON t.id = te.task_id WHERE t.plan_id = X`
* Per-worker / per-model: trivial group-by on the JOIN.

**Path B contingency (if Joe picks aggregate-only):** drop the `llm_calls` table; `task_executions` carries no token columns. Token burn comes from Anthropic API usage reports queried by session label + date range. Per-task attribution is lost; per-plan attribution can be approximated by date-range correlation. This is the schema-cheap option but the metric loses its structural value as a measurement of the architectural hypothesis.

### Rework counting

Adopt **trigger-column on task_executions** with three values:

* `initial` — coordinator's first dispatch of the task to a worker.
* `coordinator-rework` — coordinator re-briefs the same task to the same (or different) worker because of operator feedback, CI failure, or other non-architect signal.
* `architect-rework` — coordinator re-briefs because the architect issued a rework verdict on a PR.

**One row per rework cycle**, all rows sharing the same `task_id`. Per-task rework count = `COUNT(*) WHERE task_id = X AND trigger != 'initial'`. Per-plan rework = `SUM(those counts) GROUP BY plan_id`. The 'initial' row is the lifecycle's start; rework rows are the iteration tail.

This captures the architectural hypothesis directly: each rework row carries its OWN `llm_calls` rows (per Path A), so we can answer "did the second-cycle rework burn more tokens because the worker needed more context?" — a direct measurement of whether the long-lived-team context-sharing is working.

**Distinction between rework triggers is load-bearing:** a high `architect-rework` count signals the architect catches things workers miss (good); a high `coordinator-rework` count signals the coordinator's initial brief wasn't sharp enough (worth investigating). Separating the two trigger values makes the diagnostic cleanly slicable.

---

## Architect-as-auditor (new role per Joe's directive)

### Identity + substrate

* **Label**: `architect-<repo-slug>` (e.g., `architect-medicoder`, `architect-treadmill`). One architect per repo, mirroring the coordinator role.
* **Workdir**: `~/.treadmill/teams/<slug>/architect/` (parallel to the coordinator's workdir under the same team root).
* **CLAUDE.md**: separate system-prompt artifact at `~/.treadmill/teams/<slug>/architect/CLAUDE.md`. Distinct from the coordinator's CLAUDE.md — different role, different decision surface.
* **Spawned alongside coordinator + workers** when a new repo's team stands up via `treadmill repo add` (per Task F PR #275, to be extended).

### Read-only API surface

The architect is a **judgment node**, not a state writer. The coordinator remains the single writer of lifecycle state (preserves the simple data-flow invariant we converged on earlier).

Architect reads:

| Method + path | Use |
|---|---|
| `GET /api/v1/tasks/{task_id}` | The task definition the worker is implementing. |
| `GET /api/v1/task_executions?task_id=<id>` | Lifecycle state + any prior rework cycles. |
| `GET /api/v1/task_prs?repo=<r>&pr_number=<n>` | Bridge from PR → task. |
| `GET /api/v1/events?task_id=<id>` | CI signals, push events, prior architect verdicts. |
| `GET /api/v1/llm_calls?task_id=<id>` (NEW) | Per-cycle token burn for the architect's reference. |

No new mutator endpoints. The architect's relay-to-coordinator carries the verdict + remediation summary; the coordinator translates to writes.

### WS subscription

Architect subscribes to `/api/v1/dashboard/ws/events` with a new `?architect_label=<label>` filter param, mirroring the `coordinator_label` filter from PR #286. The filter resolves an event's plan via `plans JOIN team_configs ON team_configs.repo = plans.repo` and forwards when `team_configs.architect_label` matches the subscriber's label. Composes by OR with the existing three filters (`plan_ids`, `coordinator_label`, `created_by`).

`team_configs` gains:

```sql
ALTER TABLE team_configs ADD COLUMN architect_label TEXT NULL;
```

Single string per repo, nullable during bootstrap. The architect_label column is the routing key.

### Verdict relay protocol

When the architect evaluates a PR (triggered by `github.check_run` or `github.pr_opened` events arriving on its WS subscription), it produces one of two verdicts via cc-relay to the repo's coordinator:

* **APPROVE** — coordinator merges the PR via existing merge surface + closes the task_execution with status=completed.
* **REWORK** — coordinator dispatches a NEW task_execution row with `trigger='architect-rework'` + carries the architect's verdict + remediation summary as the worker brief.

The architect's relay message format (proposed):

```
[from: architect-<slug>]
[verdict: approve | rework]
[pr_number: N]
[task_id: <uuid>]

<one paragraph summary of the verdict reasoning>

<for rework: bulleted remediation list the coordinator can paste into the worker brief>
```

Verdict reasoning persists as a `task.architect_verdict` event written by the coordinator on receipt. Audit trail.

### Why the relay-based approach (vs architect writing directly to the API)

Two reasons:

1. **Single-writer invariant preserved.** Coordinator stays the only mutator of task_executions / task_prs / task_board. No multi-writer races.
2. **Architect's judgment is in-context, not in-schema.** The architect's relay carries the full verdict text + reasoning to the coordinator, which then incorporates that into the worker's next brief verbatim. If the architect wrote to the API directly, the coordinator would have to re-fetch + re-derive what the architect meant. The relay carries the meaning in-band.

---

## Open questions for Joe

Five questions that need Joe's call before the implementation plan ships. Numbered for direct response.

### Q1 — Token tracking: Path A or Path B?

(Already named earlier in this thread.) Path A = subprocess-per-task with clean per-task attribution + `llm_calls` table. Path B = aggregate-only from Anthropic API usage reports, no `llm_calls` table. My recommendation is A (preserves the metric structurally); B if A's subprocess-orchestration overhead turns out to be unworkable in practice.

### Q2 — Worker team size default

Confirmed configurable per `team_configs.worker_labels` (list length). What's the v1 default for a fresh `treadmill repo add` without a `--workers` flag? My recommendation: **2 workers per repo as v1 baseline.** Lets the coordinator parallelize without overwhelming any single worker's context. Joe's call: 1 / 2 / something else?

### Q3 — Worker specialization

`team_configs.worker_capabilities` JSONB column for routing hints (e.g., `{"worker-adam": ["backend"], "worker-bethany": ["infra"]}`) — opt-in, empty = generalist. Or do we punt specialization entirely for v1 and let coordinator route purely by worker availability?

My recommendation: ship the JSONB column but leave it empty by default in v1. Coordinator reads it as a routing hint when present; routes by availability when absent. Cheap to add; explicit opt-in for teams that want it.

### Q4 — Health bot dispatch shape (now that "kicked down the road")

Joe said health bots get a future "make the donuts" message + memory tells the coordinator what to do. Does that imply:

* (a) `schedules` table KEEP (we'll need it when health bots ship), OR
* (b) `schedules` table DELETE (we'll re-add when needed)?

Cost of keep: a small table sitting unused. Cost of delete + re-add: a migration when we re-introduce. My recommendation: **KEEP** (option a). Cheaper to leave a dead table than to migrate twice.

### Q5 — Cross-repo architect or worker sharing

The three-tier per-repo model (coordinator + architect + workers) assumes everything is repo-specific. Two edge cases to flag:

* **Cross-repo architectural questions.** Sometimes a question spans repos (e.g., medicoder ↔ medicoder-events boundary). Does the per-repo architect have a path to consult a cross-repo decision-maker? My read: no special path; cross-repo questions get escalated to Joe (or to an orchestrator session in operator-team mode) the same way per-repo decisions get escalated when an architect doesn't know. The role hierarchy: workers → coordinator → architect → operator. Cross-repo = operator.
* **Worker sharing across repos.** Could `worker-adam` serve both medicoder + treadmill? Probably not in v1 — the worker's repo-specific memory + CLAUDE.md context binds to one repo. Multi-repo workers are a future-ADR question.

Joe's call: confirm the per-repo binding everywhere; cross-repo concerns escalate to operator.

---

## Decisions captured during this thread

(For the joint memo's "Decisions captured during execution" tail.)

* **Empirical question first before schema lock.** Joe's "hold on DB design for token columns until we know" landed the principle: when a metric's collection mechanism is unverified, the schema decision waits. Generalized rule: don't bake columns for data we can't yet observe.
* **Single-writer invariant for task lifecycle state.** Coordinator is the only writer of task_executions / task_prs / task_board / events tied to its plans. Architect + workers are read + relay; the coordinator translates relay messages into writes. Preserves the simple data-flow story.
* **Three-tier per-repo team is the new role-model invariant.** coordinator-<repo> + architect-<repo> + N workers (configurable). Orchestrators (alan/bert/donna/carla) are operator-tier above the team, not in it.
* **Subprocess-per-task LLM invocation in a long-lived worker session.** The long-lived aspect is repo memory + meta-context, not a single continuous LLM context window. Each task = one subprocess invocation with fresh context. Compatible with both Path A (instrumented for tokens) and the structural framing of the role.
