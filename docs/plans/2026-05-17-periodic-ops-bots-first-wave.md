---
status: active
trigger: ADR-0035 (scheduler primitive) lands with four seed schedules in PR #132 (documentarian audit, crystallization, stuck-task sweep, o11y regression scan). PR #139's migration fix unblocks them. ADR-0035 also names ``wf-rule-corpus-health`` (weekly) as a future ops bot but ships no implementation. This plan smokes the four seeded and adds the rule-corpus-health bot.
parent: docs/adrs/0035-scheduler-primitive-for-periodic-agent-work.md
---

# Plan: Periodic ops bots — first wave

Validate that the four seeded schedules actually fire end-to-end once PR #139 lands, then ship the next named-but-unimplemented ops bot: `wf-rule-corpus-health`. Defers the monthly cost-spend report from ADR-0035 — it needs a design pass first (filed separately).

## Goal

After execution: (1) all four already-seeded schedules (`periodic-documentarian-audit`, `periodic-crystallization`, `periodic-stuck-task-sweep`, `periodic-o11y-regression-scan`) fire on their declared cadences and dispatch their bound workflows; (2) `wf-rule-corpus-health` runs weekly, auditing `docs/knowledge-base/rules/` for stale or superseded entries and opening a PR with proposed deprecations.

## Success criteria

- A one-minute test schedule (`treadmill schedules create '* * * * *' wf-documentarian-audit --jitter 0`) fires within 60s of registration, lands a `scheduled.tick` event, dispatches the bound workflow, and reaches a terminal step.
- The four seed schedules from PR #132 are visible via `treadmill schedules list` with `status=active` and have non-null `last_fired_at` within their declared cadence window.
- `role-rule-corpus-auditor` exists in `services/api/treadmill_api/starters.py` with an `output_kind: analysis` envelope and a prompt that scans `docs/knowledge-base/rules/*.yaml` against the current learning corpus.
- `wf-rule-corpus-health` workflow exists, dispatches `role-rule-corpus-auditor` step 1 + `role-code-author` step 2 (open a PR with the auditor's proposed deletes / edits), and is wired into `services/api/treadmill_api/coordination/triggers.py` so `scheduled.tick.rule-corpus-health` routes correctly.
- A `periodic-rule-corpus-health` schedule row seeded in `services/api/treadmill_api/seed/schedules.py` with cron `0 21 * * 0` (Sunday 9pm Pacific, after crystallization completes so it audits the freshest corpus).

## Constraints / scope

### In scope
- 4 tasks below.
- Smoke validation of the four seeded schedules.
- `wf-rule-corpus-health` end-to-end (role + workflow + schedule + smoke).

### Out of scope
- Monthly cost-spend report from ADR-0035 — needs design (filed as TaskList follow-up).
- Adjusting the four seeded cadences — accept as authored.
- Backfilling missed-tick semantics for any schedule — already in scheduler core (PR #128).
- Adding a UI for schedule monitoring — covered by TaskList #136 (in-flight tracking UI).

### Budget
4 days. If any single task slips past 2 days, abort and post-mortem.

## Risks / unknowns

- **Schedule firing depends on PR #139** — without the migration fix, `schedules` table doesn't exist and nothing fires. The smoke task (first in the sequence) gates on `task.wait-for-scheduler-online` confirming `schedules` table is queryable.
- **`wf-rule-corpus-health`'s output PR could deprecate rules that are actually load-bearing** — auditor mis-classifies a rule as stale. Mitigation: auditor opens a PR; operator review at merge gate catches false positives. The audit doesn't auto-delete anything.
- **Sunday-night cadence collision** — `periodic-crystallization` runs 8pm Sunday, `periodic-rule-corpus-health` proposed 9pm Sunday. If crystallization runs long, rule-corpus-health may audit a corpus mid-mutation. Mitigation: 1h gap should suffice for typical crystallization (~5-15 min); revisit if observed.

## Sequence of work

```yaml
sequence_of_work:
  - id: smoke-four-seeded-schedules
    title: Smoke — four seeded schedules fire on cadence
    workflow: wf-validate
    intent: |
      After PR #139 lands and the schedules table is live, validate
      the four already-seeded schedules from PR #132 (task eaeea5ce):

        - periodic-documentarian-audit (0 9 * * 1)
        - periodic-crystallization (0 20 * * 0)
        - periodic-stuck-task-sweep (*/10 * * * *)
        - periodic-o11y-regression-scan (*/15 * * * *)

      Confirm each:
        1. Visible via ``treadmill schedules list`` with status=active.
        2. Within the declared cadence window, ``last_fired_at`` is
           non-null and a ``scheduled.tick.<schedule_id>`` event lands
           in the events table.
        3. The bound workflow dispatches (a workflow_run row with
           workflow_id matching the schedule's binding).

      For the every-10-min and every-15-min schedules, observation
      window is one hour — at least 4 firings each. For the
      Monday-9am and Sunday-8pm schedules, document expected next
      fire and confirm last_fired_at moves in a follow-up handoff
      after natural cadence (no manual cron).

      Document in
      ``docs/handoffs/2026-05-17-seeded-schedules-smoke.md``: per
      schedule, the cadence observed, the dispatched workflow, and
      any anomalies (silent fails, missed ticks, etc.).
    scope:
      files:
        - docs/handoffs/2026-05-17-seeded-schedules-smoke.md
    validation:
      - kind: deterministic
        description: |
          Handoff exists + names each schedule + confirms fire.
        script: |
          test -f docs/handoffs/2026-05-17-seeded-schedules-smoke.md \
            && grep -qi "documentarian-audit" docs/handoffs/2026-05-17-seeded-schedules-smoke.md \
            && grep -qi "crystallization" docs/handoffs/2026-05-17-seeded-schedules-smoke.md \
            && grep -qi "stuck-task-sweep" docs/handoffs/2026-05-17-seeded-schedules-smoke.md \
            && grep -qi "o11y-regression-scan" docs/handoffs/2026-05-17-seeded-schedules-smoke.md

  - id: role-rule-corpus-auditor
    title: role-rule-corpus-auditor in starters.py
    workflow: wf-author
    intent: |
      Author ``role-rule-corpus-auditor`` in
      ``services/api/treadmill_api/starters.py``:
        - output_kind: ``analysis``
        - model: WORKER_MODEL (haiku; rules can override later)
        - system_prompt: reads ``docs/knowledge-base/rules/*.yaml``
          and ``docs/learnings/*.md``, applies these criteria per
          rule:

            * Is the rule still referenced by any active learning,
              ADR, or workflow? (grep across docs/ + services/ +
              workers/)
            * Has a newer rule superseded its scope?
            * Are the proposed remediations still implementable
              (e.g., check.sh script paths still valid)?
            * Has the underlying learning been marked obsolete?

          Returns a ``RuleCorpusAudit`` envelope with one entry per
          rule: ``{rule_slug, status: keep | deprecate | update,
          rationale, proposed_action}``.

      Add ``RuleCorpusAudit`` Pydantic model at
      ``services/api/treadmill_api/events/rule_corpus_audit.py``,
      re-exported from ``events/__init__.py``. Update test_starters.py
      to assert the role + envelope register.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/treadmill_api/events/rule_corpus_audit.py
        - services/api/treadmill_api/events/__init__.py
        - services/api/tests/test_starters.py
        - services/api/tests/test_rule_corpus_audit.py
    validation:
      - kind: deterministic
        description: |
          Role + envelope register + tests pass.
        script: |
          cd services/api && uv run pytest tests/test_starters.py tests/test_rule_corpus_audit.py -q \
            && grep -q "role-rule-corpus-auditor" treadmill_api/starters.py \
            && grep -q "RuleCorpusAudit" treadmill_api/events/rule_corpus_audit.py

  - id: wf-rule-corpus-health-workflow
    title: wf-rule-corpus-health workflow + consumer routing
    workflow: wf-author
    depends_on:
      - task.role-rule-corpus-auditor.pr_merged
    intent: |
      Define ``wf-rule-corpus-health`` workflow in starters.py:
        - step 1: role-rule-corpus-auditor (analysis)
        - step 2: role-code-author (action) — opens PR with proposed
          deprecate/update changes from step 1's envelope. PR body
          links each change to the auditor's rationale.

      Wire consumer routing in
      ``services/api/treadmill_api/coordination/triggers.py``:
      ``scheduled.tick.<schedule_id>`` whose schedule has
      ``workflow_id='wf-rule-corpus-health'`` dispatches the
      workflow with payload ``{trigger: 'scheduled-sweep'}``.

      Tests cover: workflow registers; the two steps run in
      sequence; step-1 fail → step-2 doesn't dispatch.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_starters.py
        - services/api/tests/test_consumer_unit.py
    validation:
      - kind: deterministic
        description: |
          Workflow + routing wired; tests pass.
        script: |
          cd services/api && uv run pytest tests/test_starters.py tests/test_consumer_unit.py -q \
            && grep -q "wf-rule-corpus-health" treadmill_api/starters.py \
            && grep -q "wf-rule-corpus-health" treadmill_api/coordination/triggers.py

  - id: seed-rule-corpus-health-schedule
    title: Seed periodic-rule-corpus-health schedule (weekly)
    workflow: wf-author
    depends_on:
      - task.wf-rule-corpus-health-workflow.pr_merged
    intent: |
      Add a fifth seed entry to
      ``services/api/treadmill_api/seed/schedules.py``:

        - id: ``periodic-rule-corpus-health``
          cron: ``0 21 * * 0`` (Sunday 9pm Pacific, one hour after
                                periodic-crystallization completes)
          workflow_id: ``wf-rule-corpus-health``
          quiet_hours: null
          quiet_tz: America/Los_Angeles
          jitter_seconds: 60
          payload_template: ``{"trigger": "scheduled-sweep"}``

      Idempotent — re-running seed is safe per ADR-0035. After seed,
      operator runs ``treadmill schedules list`` to confirm the new
      row, then waits one cadence cycle (or invokes a manual fire via
      ``treadmill schedules fire <id>`` if the CLI supports it).

      Tests: seed produces the new row; idempotent on re-run; CLI
      lists it.
    scope:
      files:
        - services/api/treadmill_api/seed/schedules.py
        - services/api/tests/test_seed_schedules.py
    validation:
      - kind: deterministic
        description: |
          Seed + tests pass.
        script: |
          cd services/api && uv run pytest tests/test_seed_schedules.py -q \
            && grep -q "periodic-rule-corpus-health" treadmill_api/seed/schedules.py
```

## Decisions captured during execution

(empty)

## Post-mortem

Filled in on transition to `completed` / `abandoned`.
