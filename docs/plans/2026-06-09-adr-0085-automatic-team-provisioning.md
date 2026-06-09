---
auto_merge: false
---
# Plan: ADR-0085 — Automatic Team Provisioning

- **Status:** drafting
- **Date:** 2026-06-09
- **Related ADRs:** ADR-0085, ADR-0084, ADR-0050 (repo onboarding), ADR-0018 (autoscaler — retired)

## Goal

Implement ADR-0085: when a plan is submitted for a repo, Treadmill automatically routes its tasks to the repo's coordinator session — no `--created-by` flag, no manual `coordinator.env` editing, no autoscaler racing to pick up the wrong tasks.

This plan ships the three load-bearing pieces: the `team_configs` DB table, the `plan submit` routing change, and the `treadmill repo add` provisioning command that stands up a coordinator systemd service.

## Success criteria

1. `team_configs` table exists with columns `(id, repo, coordinator_label, worker_labels, created_at, updated_at)`.
2. `treadmill plan submit --repo MediCoderHQ/medicoder --doc <path>` sets `created_by = "coordinator-medicoder"` automatically (no `--created-by` flag required).
3. `treadmill repo add MediCoderHQ/medicoder` creates the `team_configs` row, writes `~/.treadmill/teams/medicoder/coordinator.env`, and issues `systemctl --user enable/start treadmill-channel@coordinator-medicoder.service`.
4. Tasks with a `created_by` matching a `team_config.coordinator_label` are filtered out of the autoscaler's SQS poll — they never appear in the autoscaler's `visible` count.
5. On plan submit, a `plan.submitted` SQS message is published to the plan queue with `coordinator_label` as a message attribute, waking the coordinator.
6. The coordinator session, on receiving `plan.submitted`, adds the plan ID to its watched-plans set (updates `coordinator.env` and reloads its config in memory).

## Constraints / scope

### In scope
- `services/api/` — `team_configs` schema, Alembic migration, model, CRUD endpoints
- `services/api/routers/` — `plan submit` routing change (auto-set `created_by`)
- `services/api/routers/` — `plan.submitted` SQS publish on plan create
- `cli/` — `treadmill repo add` provisioning command
- `tools/local-adapter/treadmill_local/autoscaler.py` — filter out coordinator-owned tasks
- Coordinator session — on-startup orphan check + `plan.submitted` handler for self-registration

### Out of scope
- Worker availability heartbeat (ADR-0085 §5) — deferred to follow-up
- DB-backed plan subscription (replacing `TREADMILL_COORDINATOR_PLANS` env var) — deferred
- Multi-team routing (one repo, multiple coordinators) — out of scope for v1
- Retiring the autoscaler entirely from `treadmill-local up` — deferred until all active repos have `team_configs` entries

### Budget
3 working days. 5 tasks; tasks A–C are parallel.

## Sequence of work

```yaml
sequence_of_work:
  - id: team-config-schema
    title: team_configs table — Alembic migration + model + CRUD
    workflow: wf-author
    depends_on: []
    intent: |
      Add the `team_configs` table to the Treadmill API database.

      1. Create an Alembic migration in `services/api/alembic/versions/` that adds:
         ```sql
         CREATE TABLE team_configs (
             id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
             repo          VARCHAR(255) NOT NULL,
             coordinator_label VARCHAR(64) NOT NULL,
             worker_labels TEXT[] NOT NULL DEFAULT '{}',
             created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
             updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
             CONSTRAINT team_configs_repo_unique UNIQUE (repo)
         );
         ```
         Follow the naming convention of existing migrations.

      2. Add `TeamConfig` SQLAlchemy model in `services/api/models/` (or `models.py`
         depending on file layout) with the same columns. Include `__tablename__ = "team_configs"`.

      3. Add `TeamConfigStore` (or inline methods on the existing store pattern) in
         `services/api/store.py` or equivalent with:
         - `get_team_config(repo: str) -> TeamConfig | None`
         - `create_team_config(repo, coordinator_label, worker_labels) -> TeamConfig`
         - `upsert_team_config(repo, coordinator_label, worker_labels) -> TeamConfig`

      4. Add a `GET /api/v1/team_configs/{repo}` endpoint and a
         `PUT /api/v1/team_configs/{repo}` endpoint (upsert). These are used by
         `treadmill repo add` and by operator tooling.

      5. Update `services/api/AGENT.md`: add `team_configs` to "Key surfaces".

      Do NOT touch `plan submit` routing yet — that is task `plan-submit-routing`.
    scope:
      files:
        - services/api/alembic/versions/
        - services/api/models.py
        - services/api/store.py
        - services/api/routers/team_configs.py
        - services/api/main.py
        - services/api/AGENT.md
        - services/api/tests/test_team_configs.py
      services_affected:
        - treadmill-api
      out_of_scope:
        - plan submit routing (task plan-submit-routing)
        - CLI provisioning (task repo-add-provision)
    validation:
      - kind: deterministic
        description: >
          Migration file exists; TeamConfig model present; CRUD endpoints registered;
          tests pass.
        script: |
          set -euo pipefail
          find services/api/alembic/versions/ -name "*.py" | xargs grep -l "team_configs" | grep -q . || (echo "FAIL: no migration for team_configs" && exit 1)
          grep -q "team_configs" services/api/models.py || grep -rq "team_configs" services/api/models/ 2>/dev/null || (echo "FAIL: TeamConfig model not found" && exit 1)
          grep -q "team_config" services/api/routers/team_configs.py || (echo "FAIL: team_configs router not found" && exit 1)
          cd services/api && python -m pytest tests/test_team_configs.py -q 2>&1 | tail -5

  - id: plan-submit-routing
    title: plan submit auto-sets created_by from team_configs; publishes plan.submitted SQS event
    workflow: wf-author
    depends_on:
      - task.team-config-schema.pr_merged
    intent: |
      Change `plan submit` so it derives `created_by` from `team_configs` rather than
      requiring the `--created-by` flag.

      In `services/api/routers/plans.py` (or wherever `POST /api/v1/plans` is handled):

      1. On plan creation, look up `team_configs` for the submitted `repo`. If a record
         exists, set `created_by = team_config.coordinator_label`. If no record exists,
         fall through to the existing behavior (caller-supplied `created_by` or `None`).

      2. After the plan row and task rows are created, publish a `plan.submitted` SQS
         message to the plan event queue:
         ```python
         {
           "event": "plan.submitted",
           "plan_id": str(plan.id),
           "repo": plan.repo,
           "coordinator_label": team_config.coordinator_label,
           "task_count": len(tasks),
         }
         ```
         Message attribute `coordinator_label` = coordinator label string (used by the
         coordinator's SQS filter — the coordinator subscribes to messages where
         `coordinator_label` matches its own label).

      3. Update the `treadmill plan submit` CLI to make `--created-by` optional (not
         required) when a `team_configs` entry exists for the repo. If neither
         `team_configs` nor `--created-by` is supplied, fail with a clear error:
         "No team config found for repo <repo>. Run `treadmill repo add <repo>` first
         or pass --created-by explicitly."

      4. Update `services/api/AGENT.md` and `cli/AGENT.md`: note the routing change.
    scope:
      files:
        - services/api/routers/plans.py
        - services/api/AGENT.md
        - cli/treadmill_cli/plan_submit.py
        - cli/AGENT.md
        - services/api/tests/test_plan_submit_routing.py
      services_affected:
        - treadmill-api
      out_of_scope:
        - team_configs CRUD (task team-config-schema)
        - autoscaler filtering (task autoscaler-filter)
    validation:
      - kind: deterministic
        description: >
          plan submit reads team_configs for created_by; publishes plan.submitted;
          --created-by is optional when team_configs exists.
        script: |
          set -euo pipefail
          grep -q "team_config\|team_configs" services/api/routers/plans.py || (echo "FAIL: plans.py not reading team_configs" && exit 1)
          grep -q "plan.submitted" services/api/routers/plans.py || (echo "FAIL: plan.submitted SQS event not published" && exit 1)
          grep -q "coordinator_label" services/api/routers/plans.py || (echo "FAIL: coordinator_label not in plan.submitted payload" && exit 1)
          cd services/api && python -m pytest tests/test_plan_submit_routing.py -q 2>&1 | tail -5

  - id: autoscaler-filter
    title: Filter coordinator-owned tasks from autoscaler SQS poll
    workflow: wf-author
    depends_on:
      - task.team-config-schema.pr_merged
    intent: |
      The autoscaler must not pick up tasks whose `created_by` matches a
      `team_config.coordinator_label`. This is the gate that prevents the old worker
      pool from racing against the coordinator.

      In `tools/local-adapter/treadmill_local/autoscaler.py`:

      1. On startup, load the set of `coordinator_labels` from the API:
         `GET /api/v1/team_configs` (add a list endpoint if one doesn't exist). Cache
         for 60 seconds; refresh on each tick.

      2. When polling SQS for pending tasks, filter out any task where
         `task.created_by in coordinator_labels`. The autoscaler should log at INFO
         level each time it skips a task: `skipping coordinator-owned task {task_id}
         (created_by={task.created_by})`.

      3. Update `tools/local-adapter/AGENT.md`: note the coordinator-filter behavior.

      NOTE: If the API endpoint for listing team_configs doesn't exist yet, add
      `GET /api/v1/team_configs` to the router from task `team-config-schema` scope
      (add it to scope.files here as well if needed).
    scope:
      files:
        - tools/local-adapter/treadmill_local/autoscaler.py
        - tools/local-adapter/AGENT.md
        - services/api/routers/team_configs.py
      services_affected: []
      out_of_scope:
        - Removing the autoscaler from treadmill-local up (deferred)
    validation:
      - kind: deterministic
        description: >
          autoscaler.py loads coordinator_labels from API and filters tasks by created_by.
        script: |
          set -euo pipefail
          grep -q "coordinator_label\|coordinator_labels" tools/local-adapter/treadmill_local/autoscaler.py || (echo "FAIL: autoscaler not filtering coordinator-owned tasks" && exit 1)
          grep -q "team_config\|team_configs" tools/local-adapter/treadmill_local/autoscaler.py || (echo "FAIL: autoscaler not loading team_configs" && exit 1)
          echo "OK"

  - id: repo-add-provision
    title: treadmill repo add — create team_configs row + provision coordinator systemd service
    workflow: wf-author
    depends_on:
      - task.team-config-schema.pr_merged
    intent: |
      Implement `treadmill repo add <org/repo>` as the provisioning command for
      new teams.

      In `cli/treadmill_cli/`:

      1. Create `repo_add.py` with a `repo add` subcommand:
         ```
         treadmill repo add <org/repo> [--coordinator-label <label>] [--workers bert,donna,carla]
         ```
         - Default coordinator label: `coordinator-<slug>` where slug = repo name
           lowercased with `/` replaced by `-` (e.g. `MediCoderHQ/medicoder` →
           `coordinator-medicoder`).
         - Default workers: `["treadmill-bert", "treadmill-donna", "treadmill-carla"]`.

      2. The command does the following in order:
         a. `PUT /api/v1/team_configs/{repo}` to upsert the row.
         b. Create `~/.treadmill/teams/<slug>/` directory.
         c. Write `~/.treadmill/teams/<slug>/coordinator.env`:
            ```
            TREADMILL_ROLE=coordinator
            TREADMILL_LABEL=<coordinator_label>
            TREADMILL_API_URL=http://localhost:8088
            TREADMILL_COORDINATOR_PLANS=
            ```
         d. Run `systemctl --user enable treadmill-channel@<coordinator_label>.service`
            (print a clear error if the template unit doesn't exist, not an exception).
         e. Run `systemctl --user start treadmill-channel@<coordinator_label>.service`.
         f. Print a summary: repo, coordinator label, worker pool, systemd unit status.

      3. Register `repo add` in `cli/treadmill_cli/main.py`.

      4. Update `cli/AGENT.md`: add `repo add` to "Key surfaces".

      The command is idempotent — running it twice for the same repo updates the
      team_config and restarts the coordinator if already running.
    scope:
      files:
        - cli/treadmill_cli/repo_add.py
        - cli/treadmill_cli/main.py
        - cli/AGENT.md
        - cli/tests/test_repo_add.py
      services_affected: []
      out_of_scope:
        - plan submit routing (task plan-submit-routing)
        - coordinator session code (task coordinator-plan-self-register)
    validation:
      - kind: deterministic
        description: >
          repo_add.py exists; repo add subcommand registered; calls PUT team_configs
          and writes coordinator.env.
        script: |
          set -euo pipefail
          test -f cli/treadmill_cli/repo_add.py || (echo "FAIL: repo_add.py not found" && exit 1)
          grep -q "repo.add\|repo add\|repo_add" cli/treadmill_cli/main.py || (echo "FAIL: repo add not registered in main.py" && exit 1)
          grep -q "coordinator.env\|coordinator_label" cli/treadmill_cli/repo_add.py || (echo "FAIL: coordinator.env not written" && exit 1)
          grep -q "systemctl" cli/treadmill_cli/repo_add.py || (echo "FAIL: systemd enable/start not called" && exit 1)
          cd cli && python -m pytest tests/test_repo_add.py -q 2>&1 | tail -5

  - id: coordinator-plan-self-register
    title: Coordinator handles plan.submitted event — self-registers new plans without operator edit
    workflow: wf-author
    depends_on:
      - task.plan-submit-routing.pr_merged
    intent: |
      The coordinator session must handle the `plan.submitted` SQS event to add new
      plan IDs to its watched set without requiring operator edits to `coordinator.env`.

      The coordinator session is a Claude Code process launched by `launch-session.sh`
      (or the systemd unit). It receives treadmill-events channel notifications. The
      `plan.submitted` SQS event (published by task `plan-submit-routing`) arrives as
      a channel notification when the coordinator's SQS filter matches on
      `coordinator_label`.

      In the coordinator session startup instructions (the CLAUDE.md or session prompt
      for coordinator-labelled sessions — check
      `~/.claude/projects/-home-joe-treadmill/` or the systemd unit's exec args for
      where this is defined):

      1. Add a handler for `plan.submitted` events:
         - Parse `plan_id` and `coordinator_label` from the event payload.
         - If `coordinator_label` matches `TREADMILL_LABEL`, append `plan_id` to
           `TREADMILL_COORDINATOR_PLANS` in the coordinator's env file
           (`~/.treadmill/teams/<slug>/coordinator.env`).
         - Immediately read the task board for the new plan and begin routing tasks
           to available workers.

      2. Add an orphan-recovery check on coordinator startup:
         - Query `GET /api/v1/tasks?created_by=<coordinator_label>&status=wf-author:pending`
           (or equivalent — tasks that belong to this coordinator but have no
           `task_board` entry).
         - For each orphaned task, add its plan to `TREADMILL_COORDINATOR_PLANS` if
           not already present, then begin routing.
         - Log at INFO level: `recovered N orphaned tasks from M plans`.

      3. Document the `plan.submitted` handler in the coordinator's AGENT.md or
         session instructions. Create/update
         `docs/adrs/0085-automatic-team-provisioning.md` status to `accepted` now
         that the implementation is complete.

      NOTE: The coordinator session code may live in a CLAUDE.md file, a skill,
      or inline session-start instructions. Locate the right file by checking
      `~/.claude/projects/` and `treadmill/.claude/skills/` for coordinator-role
      definitions. Update wherever the coordinator session instructions are defined.
    scope:
      files:
        - .claude/skills/coordinator/SKILL.md
        - docs/adrs/0085-automatic-team-provisioning.md
      services_affected: []
      out_of_scope:
        - Worker availability heartbeat (ADR-0085 §5 — deferred)
    validation:
      - kind: llm-judge
        description: >
          The coordinator session instruction file (SKILL.md or CLAUDE.md) describes
          handling for plan.submitted events and startup orphan recovery.
        prompt: >
          Does the changed coordinator instruction file (1) describe how to handle
          a plan.submitted event by appending the plan_id to TREADMILL_COORDINATOR_PLANS
          and immediately routing its tasks, AND (2) describe a startup orphan-recovery
          check that queries for tasks with matching created_by and no task_board entry?
          Both behaviors must be present and clearly described.
```

## Operator checklist (post-merge, requires credentials)

1. Run the Alembic migration: `docker exec treadmill-api alembic upgrade head`
2. Register the medicoder team: `treadmill repo add MediCoderHQ/medicoder`
3. Verify `coordinator-medicoder` systemd service is active: `systemctl --user status treadmill-channel@coordinator-medicoder.service`
4. Re-submit the scheduler migration plan (which was cancelled): `treadmill plan submit --repo MediCoderHQ/medicoder --doc .treadmill-docs/MediCoderHQ/medicoder/plans/2026-06-09-scraper-v2-scheduler-gcp.md` — no `--created-by` flag needed.
5. Verify the autoscaler does NOT pick up the new tasks (check autoscaler log: `visible` count should stay 0 for coordinator-owned tasks).
6. Verify coordinator-medicoder receives the `plan.submitted` event and begins briefing workers.

## Risks / unknowns

- **Coordinator SKILL.md location**: Task `coordinator-plan-self-register` needs to find the right file. The coordinator may be defined in `.claude/skills/`, a per-session CLAUDE.md, or the systemd unit's exec args. If the coordinator instruction surface is not a single file, this task needs to adapt.
- **SQS filter for plan.submitted**: The coordinator's current SQS filter is keyed on `created_by`. The new `plan.submitted` event uses `coordinator_label` as a message attribute. Verify the SQS filter expression covers both attributes, or update it.
- **`treadmill-local up` still starts the autoscaler**: Until all repos have `team_configs` entries, the autoscaler should keep running for the fallback case. The filter (task `autoscaler-filter`) handles the coexistence safely.

## Decisions captured during execution

*Populated as tasks run.*

## Post-mortem

*Filled in on completion.*
