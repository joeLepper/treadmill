---
auto_merge: false
status: active
---

# Plan: Make the scheduler load-bearing (finish ADR-0035's periodic bots)

- **Status:** active
- **Date:** 2026-05-22
- **Related ADRs:** ADR-0035 (scheduler primitive — still `proposed`), ADR-0034 (crystallization), ADR-0030 (docs-current-with-pr)
- **Supersedes (continues):** 2026-05-17-periodic-ops-bots-first-wave (never executed)

## Goal

Fix a **silent failure**: the scheduler ticks (1,099 ticks, `last_fired_at`
advancing) but **0 of 783 `workflow_runs` ever came from a schedule** — every
periodic bot fires into the void. Root cause for dispatch: `workflow_runs.task_id`
is still `NOT NULL`, so the taskless scheduled-dispatch path
(`_create_and_publish_run_without_task`) dies on an IntegrityError; the migration
its own docstring calls for was never written. P1 here is that migration — the
keystone that turns the scheduler from decorative into load-bearing (and lets the
already-built crystallization bot, ADR-0034, start running). Follow-on waves
build the first real health bot (`wf-stuck-task-sweep`) and the rest.

## Success criteria

- `workflow_runs.task_id` is **nullable**; a `WorkflowRun` with `task_id=None`
  persists against a real Postgres.
- After deploy, a **scheduled tick produces a real `workflow_run`** (verified on
  an existing scheduled workflow — e.g. crystallization) instead of being
  dropped — the operator confirms end-to-end before declaring P1 done.
- Task-centric views (task_status, mergeability) are unaffected by taskless runs.

## Constraints / scope

### In scope (this wave)
P1 only — the `workflow_runs.task_id` nullable migration + model change + tests +
the AGENT.md update.

### Out of scope (follow-on waves)
P2 `wf-stuck-task-sweep` (the first new health bot), P3 o11y-regression-scan +
DLQ alerting, P5 rule-corpus-health scheduling, P6 crystallization auto-dispatch
(unblocks for free once P1 lands). The missing bot *workflows* are separate
builds; this wave only unblocks the dispatch mechanism.

### Budget
One task. Manual-merge (`auto_merge: false`): core-table schema change + a
second session is active in `services/api`; the operator runs the migration +
an end-to-end scheduled-dispatch smoke locally before merging.

## sequence_of_work

```yaml
sequence_of_work:
  - id: workflow-runs-task-id-nullable
    title: Make workflow_runs.task_id nullable (unblock scheduled dispatch, ADR-0035)
    workflow: wf-author
    intent: |
      Scheduled (taskless) workflow dispatch is silently broken: the scheduler
      ticks but no scheduled `workflow_run` ever persists, because
      `workflow_runs.task_id` is `NOT NULL` and the scheduled path creates a run
      with `task_id=None`. Make the column nullable. Read first:
      `services/api/treadmill_api/models/run.py` (the `WorkflowRun` model;
      `task_id` is at line ~34, `Mapped[uuid.UUID]` with `nullable=False`) and
      `services/api/treadmill_api/coordination/triggers.py`
      (`_create_and_publish_run_without_task`, whose docstring states this
      migration is the precondition).

      (1) MIGRATION — new Alembic migration under
      `services/api/alembic/versions/`. Datetime-keyed revision id
      `YYYYMMDD_HHMM` (ADR-0044); `down_revision` = the current single head (run
      `cd services/api && uv run alembic heads`; at authoring time it is
      `20260521_1857` — verify). `upgrade()`:
      `op.alter_column("workflow_runs", "task_id", existing_type=sa.UUID(),
      nullable=True)`. `downgrade()`: set `nullable=False` (note in a comment
      that downgrade fails if taskless rows exist — acceptable). After adding,
      `uv run alembic heads` must still report exactly ONE head.

      (2) MODEL — in `run.py`, change `WorkflowRun.task_id` to
      `Mapped[uuid.UUID | None]` with `nullable=True` (keep the existing
      ForeignKey to `tasks.id`; a nullable FK is valid). Do not change the
      `WorkflowRunStep` model.

      (3) TESTS — `services/api/tests/test_workflow_run_taskless.py` (new):
        - A non-DB structural assertion (always runs):
          `from treadmill_api.models.run import WorkflowRun;
          assert WorkflowRun.__table__.c.task_id.nullable is True`.
        - An INTEGRATION test guarded by
          `@pytest.mark.skipif(not os.environ.get("TREADMILL_INTEGRATION"), ...)`
          using the real-Postgres `session_factory` pattern from
          `tests/test_integration_cross_step.py` (it runs `alembic upgrade head`
          via the `migrations_applied` fixture): insert a `WorkflowRun` with
          `task_id=None` (+ the other required fields — inspect the model for
          NOT-NULL columns like `workflow_version_id`, `trigger`), commit, read
          it back, assert `task_id is None`. (Don't fabricate FKs the model
          doesn't require; if `workflow_version_id` is a NOT-NULL FK, insert a
          minimal parent row or use an existing fixture pattern.)

      (4) DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update
      `services/api/AGENT.md` — note `workflow_runs.task_id` is now nullable to
      support **taskless scheduled runs** (ADR-0035 periodic dispatch), in
      Key surfaces / Recent changes.
    scope:
      files:
        - services/api/alembic/versions/
        - services/api/treadmill_api/models/run.py
        - services/api/tests/test_workflow_run_taskless.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/coordination/triggers.py
    validation:
      - kind: deterministic
        description: |
          Single alembic head; the model column is nullable; tests pass
          (integration skips without TREADMILL_INTEGRATION).
        script: |
          cd services/api \
            && [ "$(uv run alembic heads | grep -c '(head)')" = "1" ] \
            && uv run python -c "from treadmill_api.models.run import WorkflowRun; assert WorkflowRun.__table__.c.task_id.nullable is True" \
            && uv run pytest tests/test_workflow_run_taskless.py -q
```

## Risks / unknowns

- **Taskless runs in task-centric joins:** task_status / mergeability join on
  `task_id`; a NULL won't match → taskless scheduled runs are correctly excluded
  (verify nothing assumes a non-null `task_id` and crashes — the operator's
  end-to-end smoke covers this).
- **Concurrent session in `services/api`:** if it lands a migration first, rebase
  the chain at merge (the ADR-0045 single-head gate catches collisions).
- **DB tests don't run in CI:** the operator runs
  `TREADMILL_INTEGRATION=1` tests + the end-to-end scheduled-dispatch smoke
  before merging (why this is manual-merge).

## Decisions captured during execution

- **One migration unblocks the whole periodic-bot layer** — the scheduler was
  decorative (0 scheduled runs of 783); this is the keystone, then the bots
  themselves follow.

## Post-mortem

_(filled when the wave completes)_
