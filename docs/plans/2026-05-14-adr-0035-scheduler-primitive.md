---
status: drafting
trigger: ADR-0035 accepted 2026-05-14. Drafted same-day. **Held — DO NOT submit via CLI** until ADR-0031 hands-free driving lands per memory/feedback_dont_compound_during_migration.md.
parent: docs/adrs/0035-scheduler-primitive-for-periodic-agent-work.md
---

# Plan: Scheduler primitive (ADR-0035 execution)

Port RAMJAC's scrape-scheduler pattern in-tree: cron schedules → tick events → bound workflow dispatch, with jitter + quiet hours + 4h missed-tick catch-up. Unlocks ADR-0032 Q32.f (periodic documentarian audit) + ADR-0034 Q34.d (periodic learnings crystallization).

## Goal

After execution: operator creates a schedule via `treadmill schedules create '0 9 * * 1' wf-documentarian-audit`, the scheduler subprocess fires `scheduled.tick.<schedule_id>` every Monday at 9am Pacific (with jitter), the consumer dispatches `wf-documentarian-audit`, and the documentarian workflow runs as if dispatched directly. Quiet hours + missed-tick catch-up behave per the RAMJAC feature set (`../ramjac/service/scrape_scheduler/src/scheduler.py` at commit `2b9e9cead^`).

## Success criteria

- `schedules` table exists via Alembic migration; rows carry `id`, `cron_expression`, `workflow_id`, `payload_template` (JSON), `status`, `jitter_seconds`, `quiet_hours`, `quiet_tz`, `quiet_multiplier`, `last_fired_at`, `created_by`, `created_at`.
- `services/api/treadmill_api/scheduler/` (new package) implements the cron-tick loop with deterministic hash-based jitter + quiet-hour multiplier + 4h missed-tick catch-up, ported from RAMJAC.
- Scheduler spawns as a sibling subprocess of the autoscaler when `treadmill-local up` runs in dev-local mode (per ADR-0018 precedent).
- `treadmill schedules list / create / pause / resume / delete` work end-to-end against the live API.
- Consumer's trigger evaluator recognizes `scheduled.tick.<schedule_id>` events + dispatches the bound workflow.
- Two seed schedules registered on first deploy: `wf-documentarian-audit` (weekly Monday 9am Pacific) + `wf-crystallize-learning` (weekly Sunday 8pm Pacific).
- Smoke: create a one-off test schedule firing in the next minute; watch the tick event land + the bound workflow dispatch; observe Grafana traces (ADR-0020) carry the schedule span.

## Constraints / scope

### In scope

- 7 tasks below.
- Build in-tree per Q35.a (operator inclined; prior art in RAMJAC).
- Feature parity with the RAMJAC scheduler at commit `2b9e9cead^`: jitter, quiet hours, quiet multiplier + cap, missed-tick catch-up within 4h.

### Out of scope

- Adopting APScheduler / OpenClaw / Temporal as the engine (Q35.a resolved: build).
- Periodic dispatch in fully_local (moto) mode — the scheduler subprocess is dev-local + fully-remote only. Fully-local stays event-driven without periodic.
- Schedule editing (CLI patches the row but doesn't migrate state mid-run; if you change a schedule's cron you delete + recreate it).
- Distributed coordination (multi-scheduler) — single subprocess for v1.

### Budget

One operator session for review + dispatch + smoke. **NOT dispatched until hands-free**.

## Diagram

See ADR-0035 §Diagram for the fire-then-dispatch flow.

## Risks / unknowns

- **Cron parsing edge cases** (DST transitions, leap-second weirdness). Mitigation: use `croniter` (battle-tested PyPI lib) for the cron evaluator; offload the gnarly bits.
- **Missed-tick semantics under scheduler restart**. Mitigation: each scheduler tick reads `last_fired_at` from DB; on startup, replays any tick whose expected fire time is within 4h of now (`now - last_fired_at < 4h AND now - expected_fire_time > 0`). Beyond 4h, drop with a log.
- **Time-of-day vs UTC bugs**. Mitigation: store `quiet_tz` per-schedule explicitly; never assume UTC; test parametrically over a few timezones including DST-affected ones.
- **Quiet-hour off-by-one** (RAMJAC's `is_quiet` had `start <= hour < end` semantics + wraparound). Mitigation: copy RAMJAC's tests directly.

## Sequence of work

```yaml
sequence_of_work:
  - id: schedules-table-and-migration
    title: schedules table + Alembic migration + SQLAlchemy model
    workflow: wf-author
    intent: |
      Author ``services/api/treadmill_api/models/schedule.py``
      with the ``Schedule`` SQLAlchemy model. Fields per ADR-0035
      §Decision plus the RAMJAC-pattern feature columns:
        - id (UUID), cron_expression (str), workflow_id (str),
          payload_template (JSONB), status (active|paused),
          jitter_seconds (int, default 60),
          quiet_hours (str | null, e.g. "20-4"),
          quiet_tz (str, default America/Los_Angeles),
          quiet_multiplier (float, default 6.0),
          quiet_max_seconds (int, default 43200),
          last_fired_at (timestamp | null),
          created_by (str), created_at (timestamp).

      Alembic migration at
      ``services/api/alembic/versions/0013_schedules.py``.

      Tests: model loads; CRUD round-trips; status enum
      validates.
    scope:
      files:
        - services/api/treadmill_api/models/schedule.py
        - services/api/treadmill_api/models/__init__.py
        - services/api/alembic/versions/0013_schedules.py
        - services/api/tests/test_schedule_model.py
    validation:
      - kind: deterministic
        description: |
          Model + migration + tests.
        script: |
          cd services/api \
            && test -f treadmill_api/models/schedule.py \
            && grep -q "class Schedule" treadmill_api/models/schedule.py \
            && grep -q "jitter_seconds" treadmill_api/models/schedule.py \
            && grep -q "quiet_hours" treadmill_api/models/schedule.py \
            && grep -q "quiet_multiplier" treadmill_api/models/schedule.py \
            && uv run alembic upgrade head \
            && uv run pytest tests/test_schedule_model.py -q

  - id: scheduler-core-port-ramjac
    title: scheduler core — port RAMJAC's jitter + quiet hours + missed-tick
    workflow: wf-author
    depends_on:
      - task.schedules-table-and-migration.pr_merged
    intent: |
      Author ``services/api/treadmill_api/scheduler/`` package
      with three modules:

        - ``cron.py``: thin wrapper around ``croniter`` —
          next_fire_time(cron, after) + iter_fires(cron, start, end).
        - ``policy.py``: ports RAMJAC's
          ``calculate_jitter_seconds`` (deterministic sha1-based,
          [-amp, +amp]), ``is_quiet`` (hour-of-day window with
          wraparound), ``next_interval_seconds`` (exponential
          backoff with cap + jitter), ``quiet_window_end_epoch``.
          Tests cover RAMJAC's exact behavior — including the
          wraparound case and the quiet-multiplier cap.
        - ``runner.py``: the main loop. Every 30s:
          (a) SELECT active schedules whose next fire (per cron +
          jitter) is <= now; (b) for each, publish
          ``scheduled.tick.<schedule_id>`` event with the
          rendered payload_template; (c) UPDATE last_fired_at.
          On startup, replay missed ticks within the 4h window
          (per Q35.c).

      Reference (concrete path):
      ``../ramjac/service/scrape_scheduler/src/scheduler.py``
      at commit ``2b9e9cead^`` (PR #489 in RAMJAC's repo).
    scope:
      files:
        - services/api/treadmill_api/scheduler/__init__.py
        - services/api/treadmill_api/scheduler/cron.py
        - services/api/treadmill_api/scheduler/policy.py
        - services/api/treadmill_api/scheduler/runner.py
        - services/api/tests/test_scheduler_policy.py
        - services/api/tests/test_scheduler_runner.py
    validation:
      - kind: deterministic
        description: |
          Scheduler package + policy + runner tests; jitter
          deterministic; quiet hours wraparound correct.
        script: |
          cd services/api \
            && test -f treadmill_api/scheduler/policy.py \
            && grep -q "calculate_jitter" treadmill_api/scheduler/policy.py \
            && grep -q "is_quiet" treadmill_api/scheduler/policy.py \
            && grep -q "missed.tick\|catch_up\|catchup" treadmill_api/scheduler/runner.py \
            && uv run pytest tests/test_scheduler_policy.py tests/test_scheduler_runner.py -q

  - id: scheduled-tick-trigger-routing
    title: Consumer routes scheduled.tick.<schedule_id> → bound workflow
    workflow: wf-author
    depends_on:
      - task.scheduler-core-port-ramjac.pr_merged
    intent: |
      Extend
      ``services/api/treadmill_api/coordination/triggers.py``
      to recognize ``scheduled.tick`` event_type. On receipt:
        - Look up the schedule row by ``schedule_id`` in payload.
        - Resolve the schedule's workflow_id.
        - Dispatch the workflow using the existing
          ``Dispatcher.dispatch_task`` path (or a new
          ``dispatch_workflow_directly`` if the existing path
          requires a task context — schedules don't have tasks
          inherently). The plan accepts whichever shape is
          cleanest; the disposition lives in this task's
          implementation.

      Tests in ``test_consumer_unit.py``: synthetic
      scheduled.tick fires the bound workflow's first step.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_consumer_unit.py
    validation:
      - kind: deterministic
        description: |
          Consumer routes ticks to workflows; tests pass.
        script: |
          cd services/api \
            && grep -qE "scheduled.tick|maybe_dispatch_scheduled" treadmill_api/coordination/triggers.py \
            && uv run pytest tests/test_consumer_unit.py -q

  - id: scheduler-spawn-on-up
    title: treadmill-local up spawns the scheduler subprocess (dev-local)
    workflow: wf-author
    depends_on:
      - task.scheduler-core-port-ramjac.pr_merged
    intent: |
      In ``tools/local-adapter/treadmill_local/runtime.py``, add
      ``_start_scheduler_dev_local`` mirroring
      ``_start_autoscaler_dev_local`` (per ADR-0018 precedent):
        - Spawn ``python -m
          treadmill_api.scheduler.runner`` as a sibling subprocess.
        - Track pid in ``.treadmill-local/scheduler.pid``.
        - Log to ``.treadmill-local/scheduler.log``.
        - Restart on crash (or fail loudly).
        - ``--no-scheduler`` opt-out flag.

      Add to ``treadmill-local down`` so cycle reaps the
      subprocess cleanly.

      Fully-local (moto) mode does NOT spawn the scheduler
      (per Out-of-scope).
    scope:
      files:
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/treadmill_local/cli.py
        - tools/local-adapter/tests/test_runtime_dev_local.py
        - tools/local-adapter/tests/test_image_build.py
    validation:
      - kind: deterministic
        description: |
          Subprocess spawn wiring + opt-out flag; tests pass.
        script: |
          cd tools/local-adapter \
            && grep -q "_start_scheduler_dev_local" treadmill_local/runtime.py \
            && grep -q "no.scheduler" treadmill_local/cli.py \
            && uv run pytest tests/test_runtime_dev_local.py tests/test_image_build.py -q

  - id: schedules-cli
    title: treadmill schedules CLI — list / create / pause / resume / delete
    workflow: wf-author
    depends_on:
      - task.schedules-table-and-migration.pr_merged
    intent: |
      Add the schedules subcommand surface (Q35.e) to
      ``cli/treadmill_cli/commands/schedules.py``:

        - ``treadmill schedules list`` — table of active +
          paused schedules with next-fire time.
        - ``treadmill schedules create <cron> <workflow_id>``
          + flags for ``--jitter``, ``--quiet-hours``,
          ``--quiet-tz``, ``--payload`` (JSON).
        - ``treadmill schedules pause <id>`` / ``resume <id>``.
        - ``treadmill schedules delete <id>`` (confirm prompt).

      API endpoints in
      ``services/api/treadmill_api/routers/schedules.py``:
      GET /schedules, POST /schedules, PATCH /schedules/{id},
      DELETE /schedules/{id}.

      Tests for both CLI + router.
    scope:
      files:
        - cli/treadmill_cli/commands/schedules.py
        - cli/tests/test_schedules_command.py
        - services/api/treadmill_api/routers/schedules.py
        - services/api/tests/test_schedules_router.py
    validation:
      - kind: deterministic
        description: |
          CLI + router tests pass; subcommands listed.
        script: |
          cd cli && uv run pytest tests/test_schedules_command.py -q \
            && cd ../services/api && uv run pytest tests/test_schedules_router.py -q

  - id: seed-schedules
    title: Seed schedules for documentarian + crystallization
    workflow: wf-author
    depends_on:
      - task.scheduled-tick-trigger-routing.pr_merged
      - task.scheduler-spawn-on-up.pr_merged
    intent: |
      Add a seed step (alongside ``seed-starters``) that on
      first deploy creates two schedules:

        - id: ``periodic-documentarian-audit``
          cron: ``0 9 * * 1`` (Monday 9am Pacific)
          workflow_id: ``wf-documentarian-audit``
          quiet_hours: ``20-6``
          quiet_tz: ``America/Los_Angeles``
          payload_template: ``{"trigger": "scheduled-audit"}``

        - id: ``periodic-crystallization``
          cron: ``0 20 * * 0`` (Sunday 8pm Pacific)
          workflow_id: ``wf-crystallize-learning``
          quiet_hours: null
          payload_template: ``{"trigger": "scheduled-sweep"}``

      Idempotent — re-running seed is safe.
    scope:
      files:
        - services/api/treadmill_api/seed/schedules.py
        - services/api/tests/test_seed_schedules.py
    validation:
      - kind: deterministic
        description: |
          Seed creates both schedules; idempotent.
        script: |
          cd services/api && uv run pytest tests/test_seed_schedules.py -q

  - id: scheduler-smoke
    title: End-to-end smoke — create a test schedule, watch it fire
    workflow: wf-validate
    depends_on:
      - task.seed-schedules.pr_merged
      - task.schedules-cli.pr_merged
    intent: |
      Operator runs:

        treadmill schedules create '* * * * *' \
          wf-documentarian-audit --jitter 0

      (A schedule firing every minute with no jitter, for the
      smoke.) Watches:

        1. Within 60s, ``scheduled.tick.<id>`` event lands in
           the events table.
        2. wf-documentarian-audit dispatches (status moves to
           ``executing`` for its first step).
        3. The Grafana traces (ADR-0020) carry the schedule
           span — visible via ``treadmill observe`` once the
           o11y stack is live.

      Document the cycle + observed jitter behavior in
      ``docs/handoffs/2026-05-14-scheduler-first-smoke.md``.

      Cleanup: ``treadmill schedules delete <id>`` after the
      smoke completes.
    scope:
      files:
        - docs/handoffs/2026-05-14-scheduler-first-smoke.md
    validation:
      - kind: deterministic
        description: |
          Smoke handoff doc exists + cites tick + dispatch.
        script: |
          test -f docs/handoffs/2026-05-14-scheduler-first-smoke.md \
            && grep -qi "scheduled.tick" docs/handoffs/2026-05-14-scheduler-first-smoke.md \
            && grep -qi "dispatch" docs/handoffs/2026-05-14-scheduler-first-smoke.md
```

## Decisions captured during execution

(empty)

## Post-mortem

Filled in on transition to `completed`/`abandoned`.
