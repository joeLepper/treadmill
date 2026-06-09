# ADR-0086: Coordinator Owns Task Lifecycle Bookkeeping

- **Status:** accepted
- **Date:** 2026-06-09
- **Supersedes:** nothing (fills a gap left by ADR-0084)
- **Related:** ADR-0084 (coordinator-led execution model), ADR-0085 (automatic team provisioning), ADR-0018 (autoscaler — retired)

## Context

ADR-0084 defined the coordinator model: a coordinator session routes tasks to
orchestrator sessions (bert, donna, carla) via cc-relay. The orchestrators do
the technical work — write code, open PRs, run tests — and report back.

What ADR-0084 did not specify was who owns the Treadmill API bookkeeping that
tracks task progress. The original Treadmill lifecycle was built for ephemeral
Docker workers running the `wf-author` workflow:

1. The autoscaler picks up a queued task → creates a `workflow_run_steps` row
   (step `author`, status `running`)
2. The Docker worker opens a PR → the wf-author workflow registers it in
   `task_prs` via `POST /api/v1/task_prs`
3. GitHub merge webhook fires → event `github.pr_merged` is written, step
   marked `completed`, `task_status` view returns `pr_merged`
4. Downstream tasks whose `depends_on` checks for `task.X.pr_merged` unblock

None of step 1-3 happens in the coordinator model. Orchestrators work via
cc-relay and have no reason to call Treadmill's API. The result observed on
2026-06-09:

- `workflow_run_steps.author` sits `pending` with no `started_at` forever
- `task_status` view returns `wf-author: executing` (misleading — means
  "step exists and is pending", not "a worker is running")
- PR merge webhooks can't find the task (no `task_prs` row) → `github.pr_merged`
  event is never written
- `depends_on: task.X.pr_merged` never clears → downstream tasks stay blocked
- Fix required manual SQL inserts to unblock one task

The gap shows up at every PR open and merge in every coordinator-model plan.

## Decision

**The coordinator owns all Treadmill lifecycle bookkeeping for its orchestrators.**

Orchestrators remain purely technical executors. They write code, open PRs,
report back. All API interactions with Treadmill are the coordinator's job.

### 1. Step registration on assignment

When the coordinator routes a task to an orchestrator (sends a cc-relay brief),
it immediately registers the step start via the Treadmill API:

```
PATCH /api/v1/workflow_run_steps/{step_id}
  { "status": "running", "started_at": "<now>" }
```

The `step_id` is available from the task's workflow run (the coordinator
already queries `GET /api/v1/plans/{plan_id}/tasks` to build its task board).
If no run exists yet, the coordinator creates one first:

```
POST /api/v1/workflow_runs
  { "task_id": "<task_id>", "trigger": "coordinator" }
```

This gives Treadmill an accurate picture: a task is `running` when an
orchestrator has been briefed, not just when a step row exists.

### 2. PR registration on open

When an orchestrator reports back "PR #N opened on branch X for task Y", the
coordinator registers it immediately:

```
POST /api/v1/task_prs
  { "repo": "<repo>", "pr_number": N, "task_id": "<task_id>", "branch": "<branch>" }
```

The orchestrator brief template must require the orchestrator to include the
PR number and branch in its reply. The coordinator's brief-acknowledgement
handler fires the API call.

### 3. PR merge acknowledgement

When the PR is merged, one of two paths fires:

**Path A — webhook**: GitHub fires `pull_request.closed` with `merged: true`.
The Treadmill webhook handler writes `github.pr_merged` as before — now it
works because the `task_prs` row exists (step 2 above).

**Path B — orchestrator report**: The orchestrator reports "PR #N merged" via
cc-relay. The coordinator confirms with GitHub (`gh pr view N --json mergedAt`)
and then fires the merge event manually if the webhook hasn't landed:

```
POST /api/v1/events
  { "entity_type": "github", "action": "pr_merged",
    "task_id": "<task_id>",
    "payload": { "repo": "...", "pr_number": N, "merged_sha": "...",
                 "head_branch": "..." } }
```

Path A is the primary path; Path B is the coordinator's backstop for cases
where the webhook is delayed or fails.

### 4. Step completion

After the PR is merged and any post-merge validation passes, the coordinator
marks the step completed:

```
PATCH /api/v1/workflow_run_steps/{step_id}
  { "status": "completed", "completed_at": "<now>" }
```

### 5. `task_status` view fix

The `task_status` view currently returns `wf-author: executing` when a step
is `pending` — even when a `github.pr_merged` event exists. This is wrong:
`pr_merged` should take precedence. Fix the CASE ordering in the view so that
`github.pr_merged` event presence always wins over step status.

This is a DB migration (view redefinition), not a schema change.

### 6. Orchestrators have no Treadmill API responsibility

This is a firm constraint, not a preference. Orchestrators must not call
`/api/v1/task_prs`, `/api/v1/workflow_runs`, or `/api/v1/events` directly.
The coordinator is the single source of truth for Treadmill state. Allowing
orchestrators to write to Treadmill creates two writers and risks
inconsistency.

## Consequences

**What changes:**
- `coordinator_prompt.md` gains three new responsibilities: step registration
  on assign, PR registration on report, merge confirmation (paths A+B).
- `brief_worker.py` (the coordinator's brief template) must require the
  orchestrator to include PR number and branch in its acknowledgement message.
- One DB migration: `task_status` view fix for `pr_merged` precedence.
- `POST /api/v1/workflow_runs` (create a run for a task) and
  `PATCH /api/v1/workflow_run_steps/{id}` (update step status) need to exist
  or be added to the API. Check current coverage before implementing.

**What stays the same:**
- Orchestrators do not change. They write code and report back, exactly as now.
- The `task_prs` table, `github.pr_merged` events, and `depends_on` resolution
  all stay as-is — they just get populated correctly.
- The GitHub webhook handler is unchanged; it works correctly once `task_prs`
  rows exist.
- ADR-0085 (team provisioning) is unchanged; ADR-0086 is orthogonal.

**Known risks:**
- **Brief acknowledgement format**: orchestrators must reliably include PR
  number in their reply. If an orchestrator doesn't follow the template, the
  coordinator misses the registration. Mitigation: coordinator re-queries
  GitHub after 5 minutes to detect orphaned PRs.
- **Workflow run API coverage**: `POST /api/v1/workflow_runs` may not exist
  yet (the old wf-author runner created runs internally). If absent, the
  coordinator cannot register runs for tasks that have none. This needs to be
  confirmed and added if missing.
- **Step ID lookup**: the coordinator needs the `step_id` to PATCH it. The
  current task query may not return step IDs. API extension may be needed.

## Implementation order

1. DB migration: fix `task_status` view (CASE ordering)
2. API: `POST /api/v1/workflow_runs` + `PATCH /api/v1/workflow_run_steps/{id}`
   (add if absent; no-op if already present)
3. `coordinator_prompt.md`: add assign/PR-register/merge-confirm responsibilities
4. `brief_worker.py`: require PR number + branch in orchestrator acknowledgement

Steps 1-2 are Treadmill API changes (joeLepper/treadmill). Steps 3-4 are
coordinator prompt changes (tools/coordinator/). Steps 1-2 can be dispatched
as ADR-0085 follow-on tasks; steps 3-4 are operator-applied directly.
