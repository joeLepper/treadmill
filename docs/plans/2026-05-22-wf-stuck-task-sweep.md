---
auto_merge: false
status: active
---

# Plan: Build wf-stuck-task-sweep — first real self-health bot (ADR-0035 P2)

- **Status:** active
- **Date:** 2026-05-22
- **Related ADRs:** ADR-0035 (scheduler), ADR-0048 (operator escalation), ADR-0047 (prefer deterministic state over LLM), ADR-0030 (docs-current-with-pr)
- **Related plans:** 2026-05-22-make-scheduler-load-bearing (P1 — keystone, merged + live)

## Goal

Build the `wf-stuck-task-sweep` workflow so its already-seeded `*/10` schedule
produces real runs that **detect silently-stalled tasks and escalate them to the
operator**. P1 made taskless scheduled runs possible; this is the first real
self-health bot — and the end-to-end proof that scheduled dispatch now works
(every 10 min, so it's observable immediately, unlike the weekly bots).

## Success criteria

- The `*/10` `wf-stuck-task-sweep` schedule produces a real `workflow_run` on each
  tick (no longer dropped for a missing `WorkflowVersion`).
- A non-terminal task that has gone silent — a step completed with **no downstream
  step dispatched** for more than a threshold — is detected and **escalated once**
  (a `task.escalated_to_operator` event), not re-escalated every tick.
- Healthy / in-progress tasks are never flagged.

## Constraints / scope

### In scope
The `wf-stuck-task-sweep` workflow definition/registration + the deterministic
sweep + escalation, its tests, and the `services/api` AGENT.md.

### Out of scope
The scheduler primitive itself (works); `seed/schedules.py` (the schedule already
exists); auto-*recovery* of stuck tasks (this version detects + escalates only —
re-dispatch is a deliberate later decision).

### Budget
One task. Manual-merge (`auto_merge: false`): new scheduled health bot in
`services/api` coordination + a second session is active there; the operator
verifies a real `*/10` tick produces a sweep run + a correct escalation before
merging (the end-to-end proof the scheduler is load-bearing).

## sequence_of_work

```yaml
sequence_of_work:
  - id: wf-stuck-task-sweep
    title: Build wf-stuck-task-sweep — detect + escalate silently-stalled tasks (ADR-0035)
    workflow: wf-author
    intent: |
      Build the `wf-stuck-task-sweep` bot. Context: the `*/10` schedule already
      exists (`services/api/treadmill_api/seed/schedules.py`, see its comment:
      "step.completed had decision=fail without a downstream dispatch… silence is
      hours"), but there is NO `wf-stuck-task-sweep` workflow, so
      `handle_scheduled_tick` finds no `WorkflowVersion` and drops every tick.
      P1 (merged + live) made `workflow_runs.task_id` nullable, so taskless
      scheduled runs now persist.

      STUDY FIRST: `services/api/treadmill_api/scheduler/` +
      `handle_scheduled_tick` (how a scheduled tick dispatches a workflow + how
      a workflow gets a `WorkflowVersion`); how workflows/roles are registered in
      `starters.py` and seeded into `workflow_versions`; the existing escalation
      mechanism `_emit_operator_escalation` + `TaskEscalatedToOperator`
      (`coordination/triggers.py` ~line 1025, `events/task.py`); and how
      "downstream dispatch" / step lifecycle works in `coordination/` so you can
      define "stalled" precisely.

      DESIGN — prefer a DETERMINISTIC sweep (ADR-0047: replace LLM
      non-determinism with deterministic state — detecting stalls is a query, no
      LLM judgment needed). Implement:
        - Register `wf-stuck-task-sweep` so `handle_scheduled_tick` dispatches it
          (give it a `WorkflowVersion`). A role step is NOT required for a
          mechanical query+escalate — wire the sweep to run on the
          `wf-stuck-task-sweep` scheduled tick deterministically. Only introduce
          a role if the scheduler model genuinely cannot run a deterministic
          scheduled action.
        - The sweep: find non-terminal tasks whose latest `workflow_run_step`
          completed (especially `decision='fail'`) with NO downstream step
          dispatched, AND whose last activity is older than a threshold (default
          30 min; make it a module constant). Use the event/step tables — match
          how other coordination queries detect dispatch state.
        - For each stalled task, emit `task.escalated_to_operator` via the
          existing `_emit_operator_escalation` path. **Idempotent:** do NOT
          re-escalate a task that already has a `task.escalated_to_operator`
          event — one escalation per stall.

      TESTS — deterministic, no live LLM:
        - a fixture/state with a stalled task (non-terminal, last step completed
          > threshold, no downstream dispatch) → the sweep flags it and emits one
          escalation;
        - a healthy/in-progress task (recent activity or a pending downstream
          step) → not flagged;
        - idempotency: a stalled task that already has an escalation event → not
          re-escalated.
        Match the existing coordination test patterns (mock session / canned
        rows, or the integration `session_factory` if a DB is needed; integration
        tests skip without TREADMILL_INTEGRATION — the operator runs those + the
        live `*/10` smoke before merge).

      DOCS (ADR-0030 — REQUIRED): update `services/api/AGENT.md` — note
      `wf-stuck-task-sweep` (the scheduled silent-stall detector → operator
      escalation) in Key surfaces / Recent changes.
    scope:
      files:
        - services/api/treadmill_api/coordination/
        - services/api/treadmill_api/starters.py
        - services/api/tests/
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/seed/schedules.py
        - services/api/treadmill_api/scheduler/runner.py
    validation:
      - kind: deterministic
        description: |
          The sweep + wf-stuck-task-sweep registration exist and the sweep tests
          pass.
        script: |
          cd services/api \
            && grep -rq "wf-stuck-task-sweep" treadmill_api/ \
            && uv run pytest tests/ -q -k "stuck_task or stuck_sweep"
```

## Risks / unknowns

- **Workflow-model fit:** if the scheduler can only dispatch role-step workflows
  (not a deterministic scheduled action), the worker adapts — but a deterministic
  detector is strongly preferred (ADR-0047). The operator verifies the chosen
  shape via the live smoke.
- **"Stalled" precision:** the threshold + the "no downstream dispatch" signal
  must not flag tasks legitimately mid-step; the not-flagged test guards this.
- **Concurrent session in `services/api`:** scopes coordination + starters +
  tests; resolve any AGENT.md conflict at merge.

## Decisions captured during execution

- **Detect + escalate, not auto-recover** — the first version surfaces stalls to
  the operator; auto-re-dispatch is a separate, riskier decision.

## Post-mortem

_(filled when the wave completes)_
