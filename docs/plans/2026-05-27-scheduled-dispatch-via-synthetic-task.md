---
auto_merge: true
status: active
---

# Plan: Scheduled dispatch via synthetic task (close ADR-0035's 4th silent-failure)

- **Status:** active
- **Date:** 2026-05-27
- **Related ADRs:** ADR-0057 (this plan's design), ADR-0035 (the scheduler primitive being closed off).

## Goal

Close the **4th scheduler silent-failure pattern**: workers can't process
taskless workflow runs (`task_id = None`). Per ADR-0057, the fix is to
push the "taskless" concept into the scheduler tick handler — every
scheduled tick (and every operator-trigger) creates a synthetic Task and
uses the existing well-tested task-bound dispatch path.

This unblocks every cron-scheduled workflow that's currently decorative
(crystallization Sundays, judge-prompt optimizer Saturdays, future Wave 4
schedules) and the operator-trigger endpoint added in ADR-0053 Wave 3.

## Success criteria

- A `handle_scheduled_tick` for a workflow with a `WorkflowVersion`
  creates a `Task` row + dispatches via the existing task-bound path. The
  resulting `WorkflowRun` has a non-null `task_id`. Workers process it
  identically to PR-driven work.
- The operator-trigger endpoint (`POST /api/v1/workflows/{slug}/trigger`)
  uses the same synthetic-task path; payload's `repo` becomes the task's
  `repo`.
- The first scheduled fire (next Saturday's optimizer cron OR a manual
  retry-trigger before then) produces an end-to-end completion — at
  minimum `step.started` and `step.completed`, ideally a clean PR.
- Existing tests stay green. New tests cover the synthetic-task
  construction + that `workflow_runs.task_id` is non-null after dispatch.

## Constraints / scope

### In scope
Three changes in `services/api`:
1. Seed one system Plan at startup (`starters.py` or
   `seed/system_plan.py` — match the file conventions).
2. Refactor `handle_scheduled_tick` to create a Task + call
   `dispatch_task`.
3. Refactor the workflow-trigger endpoint (`routers/workflow_triggers.py`)
   to do the same.

Plus tests + AGENT.md docs.

### Out of scope
- Removing `_create_and_publish_run_without_task` entirely — that's a
  follow-on cleanup once the new path is verified end-to-end. Mark it
  deprecated in this PR; remove in the next.
- Adding a worker-queue DLQ (separate plan — discovered during the same
  investigation but not gated on this).
- The Wave 4 widening (PR #21) — that plan is unblocked by this fix but
  not part of it.
- Multi-repo system Plans — v1 uses one system Plan with `repo` from the
  dispatch payload (overrides the Plan's repo). Multi-Plan rollout when
  we have multiple deployments / downstream onboards.

### Budget
One task, `auto_merge: true`. The shape is well-understood (mirror
`dispatch_task` calls), validation script uses absolute paths + exact
test file names.

## sequence_of_work

```yaml
sequence_of_work:
  - id: synthetic-task-for-scheduled-dispatch
    title: Scheduled tick + operator trigger create a synthetic task (ADR-0057)
    workflow: wf-author
    intent: |
      Per ADR-0057, replace the taskless-dispatch path with a synthetic-
      task path. Read first:
        * ``docs/adrs/0057-scheduled-dispatch-creates-synthetic-task-per-tick.md``
          for the design rationale + decision table.
        * ``services/api/treadmill_api/coordination/triggers.py``
          ``handle_scheduled_tick`` (~line 2702) — the current taskless
          dispatch path uses ``_create_and_publish_run_without_task``
          (same file, ~line 2766). Read both.
        * ``services/api/treadmill_api/routers/tasks.py`` ``create_task``
          (~line 149) — the canonical task creation + dispatch pattern
          (dispatch_task at the end). The synthetic-task path mirrors this.
        * ``services/api/treadmill_api/routers/workflow_triggers.py`` —
          the operator-trigger endpoint added by ADR-0053 Wave 3
          (currently uses ``_create_and_publish_run_without_task``).
        * ``services/api/treadmill_api/models/plan.py`` and
          ``models/task.py`` for the row shapes.

      (1) SYSTEM PLAN SEED — add a one-time seed for a single "system"
      Plan. Pick the file convention that matches the existing seed code
      (likely a new function in ``treadmill_api/seed/`` parallel to
      ``seed_schedules``). Fields:
        - ``id``: a stable UUID (hardcode a sentinel like
          ``00000000-0000-0000-0000-000000000001`` so callers can
          reference it without a runtime lookup; or use a name-based
          lookup — pick whichever's cleaner).
        - ``repo``: the deployment's dogfood repo from settings (do NOT
          hardcode ``joeLepper/treadmill``; read from
          ``settings`` / deployment config — the value is the per-repo
          dispatch payload's source-of-truth anyway).
        - ``title``: ``"system: scheduler"``.
        - ``created_by``: ``"scheduler"``.
      Seed both via ``seed_*_if_empty`` (startup, fresh DB) and an
      operator CLI path mirroring ``treadmill workflows seed-starters``.
      Idempotent.

      (2) REFACTOR ``handle_scheduled_tick`` — when the schedule's
      ``workflow_id`` is NOT the deterministic-intercept slug
      (``wf-stuck-task-sweep``), instead of calling
      ``_create_and_publish_run_without_task``:
        - Resolve the latest ``WorkflowVersion`` for the schedule's
          ``workflow_id`` (as today; 404 if missing).
        - Construct a synthetic ``Task`` row:
          - ``plan_id``: the system Plan's id.
          - ``repo``: ``rendered_payload["repo"]`` (the operator-set
            field — per the schedule-payload-needs-repo finding).
          - ``title``: f"{trigger_kind}: {workflow_id} ({fire_at.isoformat()})"
            (e.g. ``"scheduled-tune: wf-tune-judge-prompts (2026-05-30T20:00:00+00:00)"``).
          - ``workflow_version_id``: the resolved version's id.
          - ``created_by``: ``"scheduler"``.
        - Call the existing ``dispatch_task`` path (or its equivalent at
          this layer — verify the right function to invoke from the
          coordination layer; mirror ``create_task``'s last call). This
          produces a normal task-bound ``WorkflowRun`` + step.ready that
          workers can process.

      (3) REFACTOR ``routers/workflow_triggers.py`` — same shape, but
      ``created_by="operator-trigger"`` and ``title`` from the payload
      (e.g., ``f"operator-trigger: {workflow_slug}"``). The endpoint
      returns ``{task_id, run_id}`` instead of just ``{run_id}`` (callers
      now have a task id they can inspect / retry / cancel).

      (4) ``_create_and_publish_run_without_task`` — DEPRECATE (don't
      remove yet): add a docstring at the top:
      ``"""DEPRECATED per ADR-0057 — use ``dispatch_task`` with a
      synthetic task. Kept for the in-flight cancel mechanism +
      backward compat during migration window."""``. Removal is a
      follow-on PR.

      (5) TESTS — exact-path files:
        * ``services/api/tests/test_scheduled_tick_synthetic_task.py``
          — mock the session + dispatcher; call ``handle_scheduled_tick``
          for a non-intercept schedule; assert a Task row was added
          with the expected shape (plan_id = system Plan, repo from
          payload, created_by = scheduler) and that the dispatch path
          was called.
        * ``services/api/tests/test_workflow_trigger_synthetic_task.py``
          — POST to ``/api/v1/workflows/{slug}/trigger``; assert the
          response includes ``task_id`` + the Task row exists with
          ``created_by = "operator-trigger"``.
        * ``services/api/tests/test_system_plan_seed.py`` — run the
          seed; assert exactly one system Plan exists; re-run; assert
          still exactly one (idempotent).

      (6) DOCS (ADR-0030 — REQUIRED): update ``services/api/AGENT.md`` —
      under "Key surfaces" / "Recent changes", note that scheduled +
      operator-triggered dispatch now create a synthetic task per
      ADR-0057.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/routers/workflow_triggers.py
        - services/api/treadmill_api/seed/
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_scheduled_tick_synthetic_task.py
        - services/api/tests/test_workflow_trigger_synthetic_task.py
        - services/api/tests/test_system_plan_seed.py
        - services/api/AGENT.md
      out_of_scope:
        - workers/agent/
        - cli/
        - services/api/alembic/versions/
        - tools/local-adapter/
    validation:
      - kind: deterministic
        description: |
          Three new test files exist and pass; the dispatch refactor is
          structurally present (handle_scheduled_tick creates a Task,
          workflow trigger endpoint returns task_id).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/tests/test_scheduled_tick_synthetic_task.py" ] \
            && [ -f "$ROOT/services/api/tests/test_workflow_trigger_synthetic_task.py" ] \
            && [ -f "$ROOT/services/api/tests/test_system_plan_seed.py" ] \
            && grep -q "dispatch_task\|created_by=.scheduler." "$ROOT/services/api/treadmill_api/coordination/triggers.py" \
            && cd "$ROOT/services/api" \
            && uv run pytest tests/test_scheduled_tick_synthetic_task.py tests/test_workflow_trigger_synthetic_task.py tests/test_system_plan_seed.py -q
```

## Risks / unknowns

- **Coordination-layer dispatch entrypoint:** `dispatch_task` lives on
  the `Dispatcher` class — `handle_scheduled_tick` already has a
  `dispatcher` arg, so it can call the same path `create_task` uses.
  The worker may need a small refactor if the existing `dispatch_task`
  has internal assumptions about the call-site context; if so, factor
  out a smaller core for both call sites to share.
- **Repo field for the system Plan:** v1 uses the dispatch payload's
  `repo` (per the schedule-payload-needs-repo finding); the Plan's own
  `repo` is the dogfood default. If a downstream deployment schedules
  bots, their payload `repo` overrides — no per-repo system Plan needed
  until multi-deployment surfaces it.
- **The 4 historical taskless runs** stay as orphaned rows. They're
  already cancelled or queue-stuck; leave them as legacy artifacts.

## Post-mortem

_(filled when the wave completes)_
