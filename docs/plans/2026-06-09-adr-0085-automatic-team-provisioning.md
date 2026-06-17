---
auto_merge: false
---
# Plan: ADR-0085 — Automatic Team Provisioning

- **Status:** superseded by 2026-06-09-adr-0085-0086-combined-implementation
- **Date:** 2026-06-09
- **Related ADRs:** ADR-0085, ADR-0084, ADR-0050 (repo onboarding), ADR-0018 (autoscaler — retired)

## Goal

Implement ADR-0085: when a plan is submitted for a repo, Treadmill automatically routes its tasks to the repo's coordinator session — no `--created-by` flag, no manual `coordinator.env` editing, no autoscaler racing to pick up the wrong tasks.

This plan ships the three load-bearing pieces: the `team_configs` DB table, the `plan submit` routing change, and the `treadmill repo add` provisioning command that stands up a coordinator systemd service.

## Success criteria

1. `team_configs` table exists with columns `(id, repo, coordinator_label, worker_labels, created_at, updated_at)`.
2. `treadmill plan submit --repo RAMJAC/ramjac --doc <path>` sets `created_by = "coordinator-ramjac"` automatically (no `--created-by` flag required).
3. `treadmill repo add RAMJAC/ramjac` creates the `team_configs` row, writes `~/.treadmill/teams/ramjac/coordinator.env`, and issues `systemctl --user enable/start treadmill-channel@coordinator-ramjac.service`.
4. A new API endpoint `GET /api/v1/queue_depth?exclude_coordinator_owned=true` returns task counts that exclude coordinator-owned pending tasks; the autoscaler calls this endpoint so coordinator-owned work never inflates its scaling signal.
5. On plan submit, a `plan.submitted` event is published via the existing `dispatcher.persist_and_publish()` mechanism with `coordinator_label` as a payload field, waking the coordinator.
6. The coordinator session, on receiving `plan.submitted` as a channel notification (via the treadmill-events SQS filter), tracks the plan ID in working memory and immediately begins routing its tasks to available workers.

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

      2. Add `TeamConfig` SQLAlchemy model as a new file
         `services/api/treadmill_api/models/team_config.py`, following the pattern
         of `models/run.py`. Export `TeamConfig` from `models/__init__.py`.

      3. Create `services/api/treadmill_api/team_config_store.py`, following the
         pattern of `onboarding_store.py` (same class-based store convention). Include:
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
        - services/api/treadmill_api/models/team_config.py
        - services/api/treadmill_api/models/__init__.py
        - services/api/treadmill_api/team_config_store.py
        - services/api/treadmill_api/routers/team_configs.py
        - services/api/treadmill_api/cli.py
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
          test -f services/api/treadmill_api/models/team_config.py || (echo "FAIL: TeamConfig model file not found" && exit 1)
          test -f services/api/treadmill_api/team_config_store.py || (echo "FAIL: team_config_store.py not found" && exit 1)
          test -f services/api/treadmill_api/routers/team_configs.py || (echo "FAIL: team_configs router not found" && exit 1)
          cd services/api && python -m pytest tests/test_team_configs.py -q 2>&1 | tail -5

  - id: plan-submit-routing
    title: plan submit auto-sets created_by from team_configs; publishes plan.submitted SQS event
    workflow: wf-author
    depends_on:
      - task.team-config-schema.pr_merged
    intent: |
      Change `plan submit` so it derives `created_by` from `team_configs` rather than
      requiring the `--created-by` flag.

      In `services/api/treadmill_api/routers/plans.py` (where `POST /api/v1/plans` is handled):

      1. On plan creation, look up `team_configs` for the submitted `repo` using
         `TeamConfigStore`. If a record exists, set `created_by = team_config.coordinator_label`.
         If no record exists, fall through to the existing behavior (caller-supplied `created_by`
         or `None`).

      2. After the plan row and task rows are created, publish a `plan.submitted` event
         using the existing `dispatcher.persist_and_publish()` pattern (same mechanism as
         `PlanRegistered` and `TaskRegistered` events already in this file). Payload:
         ```python
         {
           "event": "plan.submitted",
           "plan_id": str(plan.id),
           "repo": plan.repo,
           "coordinator_label": team_config.coordinator_label,
           "task_count": len(tasks),
         }
         ```
         The coordinator's SQS filter matches on `coordinator_label` in the payload.

      3. Update the `treadmill plan submit` CLI to make `--created-by` optional (not
         required) when a `team_configs` entry exists for the repo. If neither
         `team_configs` nor `--created-by` is supplied, fail with a clear error:
         "No team config found for repo <repo>. Run `treadmill repo add <repo>` first
         or pass --created-by explicitly."

      4. Update `services/api/AGENT.md` and `cli/AGENT.md`: note the routing change.
    scope:
      files:
        - services/api/treadmill_api/routers/plans.py
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
          grep -q "team_config\|team_configs" services/api/treadmill_api/routers/plans.py || (echo "FAIL: plans.py not reading team_configs" && exit 1)
          grep -q "plan.submitted" services/api/treadmill_api/routers/plans.py || (echo "FAIL: plan.submitted event not published" && exit 1)
          grep -q "coordinator_label" services/api/treadmill_api/routers/plans.py || (echo "FAIL: coordinator_label not in plan.submitted payload" && exit 1)
          cd services/api && python -m pytest tests/test_plan_submit_routing.py -q 2>&1 | tail -5

  - id: autoscaler-filter
    title: Filter coordinator-owned tasks from autoscaler depth signal via new API endpoint
    workflow: wf-author
    depends_on:
      - task.team-config-schema.pr_merged
    intent: |
      The autoscaler operates on queue depth (a count), not per-task records. Its
      `queue_depth_fn` returns `(visible, in_flight)` as integers from SQS. To
      prevent coordinator-owned tasks from inflating the autoscaler's scaling signal,
      we add a DB-backed depth endpoint that excludes coordinator-owned pending tasks,
      and point the autoscaler at it.

      STEP 1 — Add API endpoint in `services/api/treadmill_api/routers/team_configs.py`:
      ```
      GET /api/v1/queue_depth
      ```
      Response:
      ```json
      {"visible": <int>, "in_flight": <int>}
      ```
      Logic: count tasks in `wf-author:pending` (visible) and `wf-author:executing`
      (in_flight) states, EXCLUDING tasks where `created_by` matches any
      `team_configs.coordinator_label`. This is a DB query, not a live SQS poll —
      the autoscaler's SQS poll is replaced by this endpoint. The autoscaler already
      has an `httpx.Client` at line 577 of `autoscaler.py`; use it.

      STEP 2 — Update `tools/local-adapter/treadmill_local/autoscaler.py`:
      Replace `get_depth` (which calls SQS directly) with a new `get_depth_from_api`
      function that calls `GET /api/v1/queue_depth` with a 5-second timeout using
      the existing `httpx.Client`. The return shape is unchanged: `(visible, in_flight)`.
      Wire this as the new `queue_depth_fn`.

      Keep the old SQS-based `get_depth` function present but renamed to
      `get_depth_sqs` for reference; don't delete it.

      Log at INFO level when a count is fetched: `queue_depth: {visible} visible,
      {in_flight} in_flight (coordinator-owned excluded)`.

      STEP 3 — Update `tools/local-adapter/AGENT.md` and
      `services/api/AGENT.md`: note the new `/api/v1/queue_depth` endpoint and the
      autoscaler's use of it.
    scope:
      files:
        - tools/local-adapter/treadmill_local/autoscaler.py
        - tools/local-adapter/AGENT.md
        - services/api/treadmill_api/routers/team_configs.py
        - services/api/AGENT.md
      services_affected: []
      out_of_scope:
        - Removing the autoscaler from treadmill-local up (deferred)
        - Per-task SQS message inspection (not needed with this approach)
    validation:
      - kind: deterministic
        description: >
          New /api/v1/queue_depth endpoint exists in team_configs router; autoscaler
          calls the API endpoint instead of SQS directly.
        script: |
          set -euo pipefail
          grep -q "queue_depth" services/api/treadmill_api/routers/team_configs.py || (echo "FAIL: /api/v1/queue_depth endpoint not found in team_configs.py" && exit 1)
          grep -q "coordinator_label\|coordinator_owned" services/api/treadmill_api/routers/team_configs.py || (echo "FAIL: coordinator exclusion not in queue_depth endpoint" && exit 1)
          grep -q "get_depth_from_api\|queue_depth" tools/local-adapter/treadmill_local/autoscaler.py || (echo "FAIL: autoscaler not calling API depth endpoint" && exit 1)
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
           lowercased with `/` replaced by `-` (e.g. `RAMJAC/ramjac` →
           `coordinator-ramjac`).
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

      The coordinator session is a Claude Code process launched by
      `tools/coordinator/launch-coordinator.sh`. Its session instructions live at
      `tools/coordinator/coordinator_prompt.md` (introduced in PR #253 / cb31afcb).
      It receives treadmill-events channel notifications. The `plan.submitted` event
      (published by task `plan-submit-routing`) arrives as a channel notification when
      the coordinator's SQS filter matches on `coordinator_label`.

      In `tools/coordinator/coordinator_prompt.md`:

      1. Add a handler for `plan.submitted` channel notifications:
         - Parse `plan_id` and `coordinator_label` from the event payload.
         - If `coordinator_label` matches `TREADMILL_LABEL`, add the plan ID to the
           coordinator's in-memory watched-plans set (a list the coordinator maintains
           in working memory for the session lifetime — NOT by rewriting coordinator.env,
           since env vars cannot be reloaded into a running process).
         - Immediately read the task board for the new plan (`GET /api/v1/plans/{plan_id}/tasks`)
           and begin routing unassigned tasks to available workers.
         - Log: `plan.submitted received: plan_id={plan_id}, now watching N plans`.

      2. Add an orphan-recovery check on coordinator startup:
         - On startup, query the API for tasks belonging to this coordinator that have
           no task_board entry (tasks where `created_by = TREADMILL_LABEL` and status
           is `wf-author:pending` or `blocked`, with no task_board row). For each
           orphaned task, add its plan_id to the in-memory watched set and begin routing.
         - Log: `startup orphan recovery: found N orphaned tasks across M plans`.

      3. Update `docs/adrs/0085-automatic-team-provisioning.md` status to `accepted`
         now that implementation is complete (this is the final task in the chain).

      4. Update `tools/coordinator/README.md` to document the `plan.submitted` handler
         and startup orphan recovery behavior.
    scope:
      files:
        - tools/coordinator/coordinator_prompt.md
        - tools/coordinator/README.md
        - docs/adrs/0085-automatic-team-provisioning.md
      services_affected: []
      out_of_scope:
        - Worker availability heartbeat (ADR-0085 §5 — deferred)
        - Writing plan IDs to coordinator.env (not needed — tracked in working memory)
    validation:
      - kind: llm-judge
        description: >
          coordinator_prompt.md describes plan.submitted handling and startup orphan recovery.
        prompt: >
          Does the changed coordinator_prompt.md (1) describe how to handle a plan.submitted
          channel notification by adding the plan_id to an in-memory watched-plans set and
          immediately beginning task routing, AND (2) describe a startup orphan-recovery check
          that queries the API for tasks with matching created_by and no task_board entry?
          Both behaviors must be present and clearly described. The handler must NOT describe
          writing to coordinator.env or reloading env vars.
```

## Operator checklist (post-merge, requires credentials)

1. Run the Alembic migration: `docker exec treadmill-api alembic upgrade head`
2. Register the ramjac team: `treadmill repo add RAMJAC/ramjac`
3. Verify `coordinator-ramjac` systemd service is active: `systemctl --user status treadmill-channel@coordinator-ramjac.service`
4. Re-submit the scheduler migration plan (which was cancelled): `treadmill plan submit --repo RAMJAC/ramjac --doc .treadmill-docs/RAMJAC/ramjac/plans/2026-06-09-scraper-v2-scheduler-gcp.md` — no `--created-by` flag needed.
5. Verify the autoscaler does NOT pick up the new tasks (check autoscaler log: `visible` count should stay 0 for coordinator-owned tasks).
6. Verify coordinator-ramjac receives the `plan.submitted` event and begins briefing workers.

## Risks / unknowns

- **Coordinator SQS filter expression**: The coordinator's current SQS filter is keyed on `created_by`. The new `plan.submitted` event uses `coordinator_label` in the payload. Verify the filter expression covers `coordinator_label` or update it — otherwise the coordinator never receives the notification.
- **API depth endpoint vs. SQS accuracy**: `GET /api/v1/queue_depth` queries the DB, not live SQS state. If tasks are queued in SQS but not yet in the DB (race window at plan-submit time), the depth may read 0 when tasks actually exist. The window should be short (milliseconds after `persist_and_publish`) but worth noting.
- **`treadmill-local up` still starts the autoscaler**: Until all repos have `team_configs` entries, the autoscaler should keep running for the fallback case. The filter (task `autoscaler-filter`) handles the coexistence safely.
- **In-memory plan tracking doesn't survive coordinator restart**: Plans tracked in working memory are lost on restart. The orphan-recovery check on startup (coordinator-plan-self-register §2) is the only backstop. If the coordinator crashes between `plan.submitted` arrival and routing the first task, there's a gap. The mitigation is the startup orphan check — but it only finds tasks already in the DB, not ones in-flight at the exact crash moment.

## Decisions captured during execution

*Populated as tasks run.*

## Post-mortem

*Filled in on completion.*
