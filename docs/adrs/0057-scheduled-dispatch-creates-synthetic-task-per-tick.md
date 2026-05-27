# ADR-0057: Scheduled dispatch creates a synthetic task per tick

- **Status:** accepted
- **Date:** 2026-05-27
- **Amends:** ADR-0035 (the "taskless scheduled run" branch of the design).
- **Related:** ADR-0046 (operator retry), ADR-0053 (judge-prompt optimizer).

## Context

ADR-0035 introduced the scheduler primitive and a "taskless"
workflow-run shape: scheduled ticks create a `WorkflowRun` with
`task_id = NULL` and dispatch the first step. The P1 keystone
migration (2026-05-22) made `workflow_runs.task_id` nullable to support
this. The path *creates* rows fine; the dispatcher publishes
`step.ready` and sends the SQS work-queue message with
`{"task_id": null, "plan_id": null, ...}`.

But the **worker side was never updated to handle null task_id /
plan_id**. The `_Claim` dataclass annotates both as `str` (runner.py),
and downstream code (`fetch_step_context`, workspace setup, event
publishes) all assume a non-null task. Investigation 2026-05-27:

> 4 taskless runs ever created; 0 reached `running`; 0 emitted any event
> beyond `step.ready`. No `step.failed`, no `dispatch_publish_failed`.
> Workers receive the SQS message, construct a `_Claim` with `task_id =
> None` (Python dataclasses don't validate types at runtime), then die
> silently somewhere between claim and `step.started` publish.

This is the **fourth silent-failure pattern** in the scheduler primitive
([[project_health_bots_track]]: nullable task_id, missing
WorkflowVersion, missing `repo` in payload_template, now this). Each
prior pattern was a one-line / one-file fix. This one would be a
multi-file refactor of the worker — and even fixed, it leaves the
worker carrying two divergent code paths (task-bound vs taskless),
each with its own subtle invariants to maintain.

The same problem reaches the **operator-trigger endpoint** added in
ADR-0053 Wave 3 (`POST /api/v1/workflows/{slug}/trigger`) — it shares
`_create_and_publish_run_without_task` with the scheduler tick handler.

## Decision

**Schedule handlers (and the operator-trigger endpoint) create a
synthetic `Task` per dispatch and use the existing task-bound
`dispatch_task` path.** Workers never see `task_id = None` — the
"taskless" concept is collapsed into the scheduler tick handler, where
it's just "every fire creates a fresh synthetic Task tied to a system
Plan."

Concretely:

1. Seed a single **system Plan** at startup (`starters.py` /
   `seed/`): repo from deployment config (`joeLepper/treadmill` at
   dogfood; per-repo for downstream onboards), title `"system: scheduler"`,
   `created_by = "scheduler"`. One plan total. Re-seeding is a no-op.
2. In `handle_scheduled_tick` and the workflow-trigger endpoint,
   replace the call to `_create_and_publish_run_without_task` with:
   - Create a new `Task` row tied to the system Plan.
     - `title`: from the schedule (e.g. `"scheduled-tune: role-architect
       (2026-05-30T20:00)"`) or from the operator trigger's payload.
     - `repo`: from the schedule/operator payload.
     - `workflow_version_id`: resolved as today.
     - `created_by`: `"scheduler"` or `"operator-trigger"`.
   - Call the existing `dispatch_task` path — workers receive a normal
     task-bound claim.
3. **The `workflow_runs.task_id` column stays nullable** — the migration
   is irreversible on live data (the 4 historical taskless rows still
   exist). Document that the nullable column is now legacy; future
   scheduler-spawned runs always carry a task_id.
4. The old `_create_and_publish_run_without_task` function is removed
   after the migration lands; its callers are the two paths above and
   nothing else.

## Why Path B (synthetic task) over Path A (fix workers to handle None)

| | Path A — fix workers | Path B — synthetic task |
|---|---|---|
| Files changed | Worker `_Claim`, `_handle_step`, `fetch_step_context`, workspace setup, step lifecycle event payloads, API endpoints that fetch task context, all corresponding tests | One scheduler handler + one router endpoint + one Plan seed |
| New invariants | Every place that touches a step now branches on "has task?" — easy to drift | None — uniform task-bound dispatch shape |
| Workers carry | Two divergent code paths | One |
| ADR-0035's "taskless workflow run" | Preserved as a real concept | Collapsed to a scheduler-internal detail |
| Operator visibility of scheduled work | "Where did this run come from?" requires inferring from the trigger string | Scheduled work shows in the task list with a clear title + created_by |

Path B is smaller, has fewer invariants to maintain, and naturally surfaces
scheduled work to the existing operator UIs (`treadmill task list`).

## Consequences

**Pros:**
- Closes the 4th scheduler silent-failure pattern with a single-place
  change instead of a multi-file worker refactor.
- Scheduled / operator-triggered work shows up in the task list as
  inspectable, retryable, cancellable rows (all the operator-CLI
  affordances that already exist for task-bound work).
- Workers stay simple — one code path.

**Cons / open:**
- Synthetic tasks accumulate in the `tasks` table over time. Mitigated
  by `created_by = "scheduler"` for filtering + a future cleanup bot
  (out of scope here).
- The "system Plan" needs a `repo`; for multi-repo deployments
  (downstream onboards), we either create one system Plan per repo or
  make the schedule's payload-supplied `repo` the source of truth
  (current intent: latter).
- `_create_and_publish_run_without_task` removal could be a flag-day
  edit. Mitigation: keep it for the migration window; remove in a
  follow-on once the new path is verified.

## Decisions captured during execution

_(filled when the implementation plan completes)_

## Plan

See `docs/plans/2026-05-27-scheduled-dispatch-via-synthetic-task.md`.
