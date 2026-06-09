# ADR-0085: Automatic Team Provisioning on Plan and Repo Submission

- **Status:** proposed
- **Date:** 2026-06-09
- **Supersedes:** ADR-0018 (autoscaler — retired by this ADR)
- **Related:** ADR-0050 (bootstrap / repo onboarding), ADR-0073 (persistent orchestrator sessions), ADR-0084 (coordinator-led execution model)

## Context

ADR-0084 defined the coordinator model: a persistent named session acts as PM for a plan, routes tasks to worker sessions, and receives SQS events for the plan's lifecycle. The model was designed and observed to work. What it did not specify was *how coordinators come to exist* — that was left as an operator task.

The gap became concrete on 2026-06-09 when a plan (`scraper_v2_scheduler` GCP migration) was submitted via `treadmill plan submit --created-by coordinator-medicoder`. The Treadmill autoscaler, still running from the old model, immediately picked up all 8 unblocked tasks and spun up Docker workers. The operator had to manually cancel the tasks and kill the autoscaler. The coordinator-medicoder session — which was *already running as a systemd service* — never received the tasks because:

1. The plan was submitted with `--created-by coordinator-medicoder` but the autoscaler has no awareness of `created_by` routing — it picks up all pending tasks regardless.
2. There is no mechanism to declare "this repo's tasks belong to this coordinator, not to the worker pool."
3. Standing up a new team for a new repo requires operator hands: create `coordinator.env`, add plan IDs to it, enable the systemd unit, verify the SQS filter is wired correctly.

Joe's observation: "the old worker pool is inefficient and error-prone" captures this exactly. The autoscaler was designed for a world where ephemeral Docker workers are the execution primitive. That world is over.

## Decision

### 1. TeamConfig — the repo-to-coordinator binding

Introduce a `team_configs` table (and corresponding `TeamConfig` model) that binds a repo to its coordinator and available worker sessions:

```sql
CREATE TABLE team_configs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo          VARCHAR(255) NOT NULL UNIQUE,  -- e.g. "MediCoderHQ/medicoder"
    coordinator_label VARCHAR(64) NOT NULL,       -- e.g. "coordinator-medicoder"
    worker_labels TEXT[] NOT NULL DEFAULT '{}',  -- e.g. ["treadmill-bert", "treadmill-donna", "treadmill-carla"]
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`plan submit` reads `team_configs` for the target repo and:
- Sets `created_by = team_config.coordinator_label` automatically (no `--created-by` flag needed)
- Publishes a `plan.submitted` SQS event addressed to the coordinator label, carrying the plan ID and the initial task graph

The autoscaler, if running, **does not pick up tasks whose `created_by` matches a known `team_config.coordinator_label`**. This creates a clean separation: coordinator-owned tasks are invisible to the worker pool.

### 2. Coordinator is always persistent — provisioning is startup, not per-plan

Coordinators are long-running systemd services (per ADR-0073, ADR-0084). They do not start and stop per plan. A coordinator for `MediCoderHQ/medicoder` runs continuously and receives all plan events for that repo. Plans queue in SQS if the coordinator is briefly offline; they drain when it restarts.

The provisioning act is therefore **repo onboarding**, not plan submission:
- When a new repo is registered (`treadmill repo add <org/repo>`), the CLI:
  1. Creates `~/.treadmill/teams/<slug>/coordinator.env` with `TREADMILL_ROLE=coordinator`, `TREADMILL_LABEL=coordinator-<slug>`, `TREADMILL_API_URL`.
  2. Writes `TREADMILL_COORDINATOR_PLANS=` (empty; filled as plans arrive via SQS).
  3. Runs `systemctl --user enable treadmill-channel@coordinator-<slug>.service`.
  4. Runs `systemctl --user start treadmill-channel@coordinator-<slug>.service`.
  5. Inserts the `team_configs` row with the generated coordinator label and a default worker pool (configurable, defaults to `["treadmill-bert", "treadmill-donna", "treadmill-carla"]`).
- The coordinator session starts, connects to treadmill-events, and waits for work.

### 3. Coordinator self-registration of new plans

When `plan submit` publishes a `plan.submitted` SQS event, the coordinator:
- Adds the plan ID to its own `TREADMILL_COORDINATOR_PLANS` env (writes `coordinator.env`, reloads its config).
- Reads the task board for the new plan.
- Begins briefing workers.

This eliminates the operator step of manually editing `coordinator.env` and eliminates the window where tasks land in the DB before the coordinator knows about them.

### 4. Autoscaler retirement

The autoscaler (ADR-0018) is retired. Its role was to translate SQS queue depth into Docker worker instances. That translation is now the coordinator's job — and the coordinator has plan context the autoscaler never had.

**Transition:**
- Any repo with a `team_configs` entry: its tasks are coordinator-owned, autoscaler-invisible.
- Repos without a `team_configs` entry (unregistered / legacy): the autoscaler remains the fallback for now. This fallback is temporary; the target state is that all repos are registered.
- The autoscaler is removed from `treadmill-local up` once all active repos have `team_configs` entries.

### 5. Worker availability broadcast

The coordinator queries `team_config.worker_labels` to know which sessions are available for a given team. A worker session is considered available if:
- Its systemd unit is `active` (checked via `systemctl --user is-active treadmill-channel@<label>.service`), OR
- It has sent a heartbeat to the coordinator within the last 120 seconds via cc-relay.

If no workers are available, the coordinator queues the task locally and retries availability every 60 seconds. It does not spin up a new Docker container. If a task is unassigned for >10 minutes, it escalates to the operator via the hint channel (ADR-0081).

### 6. `team_configs` as onboarding output

ADR-0050 (bootstrap / repo onboarding) discovers how a repo is built and operated. The output of onboarding is a `RepoConfig` row. ADR-0085 extends that output: onboarding also creates the `team_configs` row and provisions the coordinator service. The two are separate DB rows (team identity vs. repo build config) but written in the same onboarding transaction.

## Consequences

**What changes immediately:**
- `plan submit` no longer requires `--created-by`; it is derived from `team_configs`.
- Autoscaler is stopped and removed from `treadmill-local up` for repos with team configs.
- `treadmill repo add` becomes the team-provisioning command.

**What stays the same:**
- The systemd service template `treadmill-channel@.service` is unchanged.
- SQS is still the event backbone; the coordinator subscribes to plan-scoped events exactly as ADR-0084 specifies.
- The ralph loop (wf-author → gates → auto-merge) is unchanged; the coordinator routes into it.
- Worker sessions are unchanged — they receive briefs via cc-relay and create PRs as before.

**Known risks:**
- If the coordinator session crashes between `plan submit` and adding the plan ID to `coordinator.env`, the plan is orphaned. Mitigation: the coordinator should check for unrouted plans (plans in the DB with `created_by = its label` and no `task_board` entries) on startup.
- The default worker pool (bert/donna/carla) is a convention, not a guarantee. If a worker session is offline, tasks queue silently until the operator restarts the session. The 10-minute escalation window (§5) is the backstop.
- `TREADMILL_COORDINATOR_PLANS` in `coordinator.env` is a flat comma-separated list. For coordinators with many plans, a DB-backed plan subscription is cleaner. Deferred to a follow-up; the env var approach is adequate for the current scale (< 20 active plans per coordinator).
