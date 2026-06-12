---
auto_merge: false
---

# Plan: ADR-0091 — queue-driven single-active-team scheduler

- **Status:** drafting
- **Date:** 2026-06-12
- **Related ADRs:** ADR-0091 (this implements it), ADR-0090 (wake filter —
  shipped; lets a team run cheap), ADR-0087 (team model), ADR-0073
  (per-label session-id resume)

## Goal

Automate "one software team at a time": the team that owns the plan at the
head of the queue runs; the others pause. Replaces the operator pausing
teams by hand (2026-06-12). Keep concurrent opus burn bounded while letting
the active team make full progress.

## Success criteria

- Exactly one software team's units are active at a time (plus the
  always-on control plane: orchestrators, the API/ci-observer, the operator).
- The active team is the one owning the highest-priority active plan with
  pending work; a starved team eventually wins (aging), and the active team
  doesn't flip more than once per hysteresis window.
- A team is paused only at a safe point — no task `executing`, no PR in
  **await-CI** or await-merge, and its coordinator inbox drained — and
  resumes with full context (`--resume`) + a `catch_up` reconcile, so
  nothing in flight during a pause is stranded.
- An intentional pause never lands a unit in `failed` state.
- **Fail-safe:** if the scheduler decision is unavailable (API unreachable,
  error, or null), the daemon holds the current active set — it never pauses
  fleet-wide on a missing decision.

## Constraints / scope

### In scope
The decision logic (which team should be active, is a team quiescent) in the
API; a host team-control mechanism; a host scheduler daemon that reconciles;
the `SuccessExitStatus` unit fix.

### Out of scope
Cross-repo dependency modeling beyond queue order; multi-account fan-out;
scheduling the orchestrators/operator (always-on). The cron scheduler
(`scheduler/runner.py`) is unrelated and untouched.

### Budget
~4 worker-days. Abort to a post-mortem if safe-point detection can't be made
reliable from existing task/PR state without new schema.

## Sequence of work

```yaml
sequence_of_work:
  - id: scheduler-decision-api
    title: "API: desired-active-team + team-quiescence decision endpoint"
    workflow: wf-implement
    depends_on: []
    intent: |
      Add the SCHEDULER DECISION LOGIC to services/api (all testable logic
      lives here; the host daemon stays thin). New endpoint
      ``GET /api/v1/scheduler/decision`` returning JSON:
      ``{desired_team: <repo-slug>|null, quiescent_teams: [<slug>...],
      reason: str}``.
      - desired_team = the team (repo-slug) owning the highest-priority
        ACTIVE plan that has PENDING (non-terminal) work. Apply AGING so a
        team waiting longest gains priority over time (document the formula;
        a simple wait-weighted score is fine) — no team starves.
      - quiescent_teams = teams safe to pause: NO task ``executing`` AND NO
        task_pr in await-CI OR await-merge AND no half-registered PR for that
        repo. (Carla #342: a PR awaiting CI after a rework push — worker
        pushed + exited, not yet await-merge — otherwise reads quiescent and
        gets paused mid-CI; await-CI MUST count as non-quiescent.) Pure read
        over plans / tasks / task_prs.
      - null desired_team when no team has pending work.
      - aging time-constant MUST be >= the daemon's hysteresis dwell so
        aging can't out-pace anti-flap (Carla #342); document the value.
      Keep the decision a PURE function over fetched rows so it is fully
      unit-tested; the endpoint is a thin wrapper.
      Tests (services/api): two teams with pending work -> higher-priority
      wins; aging flips a long-starved team; a team mid-execute, mid-await-CI,
      OR mid-await-merge is NOT quiescent; empty queue -> null.
      Update services/api/AGENT.md.
    scope:
      files:
        - services/api/treadmill_api/routers/
        - services/api/tests/
        - services/api/AGENT.md
    validation:
      - kind: deterministic
        description: scheduler decision logic unit tests pass (priority, aging, quiescence, empty)
        script: cd services/api && uv run pytest -k "scheduler_decision or desired_team or quiescen" -q

  - id: team-control-and-unit
    title: "Host: team-control script + SuccessExitStatus unit fix"
    workflow: wf-implement
    depends_on: []
    intent: |
      Add a ``treadmill-team-control <activate|pause> <repo-slug>`` script
      under tools/cc-channels/systemd/ that starts (activate) or stops
      (pause) the systemd --user units for a team's labels
      (coordinator-<slug>, evaluator-<slug>, worker-<slug>-N), using the
      orphan-safe path (stop reaps via the deployed ExecStopPost; start
      resumes via the launcher). It NEVER touches orchestrator/operator
      units. Pause must be safe: the script refuses to pause a team that the
      API ``GET /api/v1/scheduler/decision`` reports as NOT quiescent (poll
      the API; do not pause a busy team). Also FIX the unit: add
      ``SuccessExitStatus=143`` (SIGTERM) to
      tools/cc-channels/systemd/treadmill-channel@.service so an intentional
      stop is not recorded as ``failed`` (papercut found 2026-06-12) — and
      note the installed unit needs cp + daemon-reload (operator step).
      Keep the script thin — the decision logic lives in the API task above.
    scope:
      files:
        - tools/cc-channels/systemd/treadmill-channel@.service
        - tools/cc-channels/AGENT.md
    validation:
      - kind: deterministic
        description: unit declares SuccessExitStatus=143 + team-control script present
        script: grep -q "SuccessExitStatus=143" tools/cc-channels/systemd/treadmill-channel@.service && test -f tools/cc-channels/systemd/treadmill-team-control

  - id: scheduler-daemon
    title: "Host: scheduler daemon (reconcile active team) + launch wiring"
    workflow: wf-implement
    depends_on:
      - task.scheduler-decision-api.pr_merged
      - task.team-control-and-unit.pr_merged
    intent: |
      Add an always-on control-plane daemon (model it on the deploy-watcher
      in tools/local-adapter/treadmill_local/) that, on a loop, polls
      ``GET /api/v1/scheduler/decision`` and reconciles the running team set
      toward ``desired_team`` via ``treadmill-team-control``: pause the
      current active team ONLY when it is quiescent, then activate the
      desired team (and trigger its catch_up reconcile on resume). Apply
      ANTI-FLAP hysteresis: a minimum dwell time per active team; at most one
      switch per window. SINGLE-WRITER: refuse to run a second instance
      (single-instance lock like the deploy-watcher's #333 guard). Wire it
      into ``treadmill-local up`` (dev-local) as an opt-in subprocess
      (default OFF until the operator enables it, mirroring
      start_deploy_watcher). Decision logic is NOT duplicated here — the
      daemon only enacts the API's decision.
      LOAD-BEARING FAIL-SAFE (Carla #342): if ``GET /scheduler/decision`` is
      unreachable, errors, or returns null desired_team, the daemon HOLDS the
      current active set and pauses NOTHING — never a fleet-wide pause on a
      missing decision. The API is a SPOF (a ~9h outage occurred 2026-06-12);
      the scheduler must degrade to "leave things as they are," not "stop
      everything." Pin with a test: unreachable / error / null -> zero
      team-control pause calls. Update tools/local-adapter/AGENT.md.
    scope:
      files:
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/AGENT.md
    validation:
      - kind: deterministic
        description: daemon module present + wired into up; polls the decision endpoint
        script: grep -rqiE "scheduler.decision|team.control|team.scheduler" tools/local-adapter/treadmill_local/ && grep -qiE "team.scheduler|start_team_scheduler" tools/local-adapter/treadmill_local/runtime.py
```

## Risks / unknowns

- **Safe-point reliability** is load-bearing: pausing mid-await-merge risks
  the orphan-PR class. The quiescence check (task A) must read executing +
  await-merge + inbox state accurately; pinned by tests.
- **Flapping**: two teams alternating each cycle wastes context. Hysteresis
  (task C) bounds it; tune the dwell window.
- **local-adapter sandbox tooling**: the daemon gate is a coarse grep, not a
  pytest run, because tools/local-adapter may not be pip-installed in the
  agent sandbox (the bun-class risk from ADR-0090). The REAL test of the
  decision logic is task A's pytest in services/api; the daemon is thin.
- **Default-off**: ship the daemon disabled; the operator enables it after a
  manual dry-run, so a bad reconcile can't pause the fleet unattended.

## Diagram

Intent layer captured in ADR-0091; this plan is the task sequencing.
