---
auto_merge: false
---

# Plan: ADR-0058 finalize — Steps 4, 5, 6

- **Status:** completed
- **Date:** 2026-05-28
- **Related ADRs:** ADR-0058
- **Supersedes:** none

## Goal

Finish the remaining 3 steps of the ADR-0058 plan as Treadmill-dispatched
worker tasks. Steps 1–3 (schema, prompt, dispatch handler) shipped via
hand-author this morning (PRs #50, #51, #53). What remains is a stderr-
capture lock-in, a dashboard server-side filter for the new `reason`
field, and an integration smoke covering the architect → escalation
chain end-to-end.

`auto_merge: false` because a second orchestrator is active on RAMJAC
work in parallel — every PR from this plan gets a human-eye review +
merge to avoid silent cross-session conflicts. Each task is independent
(no `depends_on` between them) so workers can pick them up in any order.

## Success criteria

- Step 4 ships a `test_step_output_log_excerpt_reaches_architect` that
  proves `validation_runtime.run_deterministic`'s `log_excerpt` field
  reaches the architect's prompt input verbatim (no upstream truncation
  beyond the 4000-char cap).
- Step 5 ships a `?reason=...` query-param filter on `/api/v1/dashboard/overview`
  that narrows the `escalations` list to a single reason value
  (`architect_cap` / `stuck_task_sweep` / `gate-broken`).
- Step 6 ships an integration test that drives a synthetic
  `step.completed` payload with `verdict='gate-broken'` through the
  consumer and verifies a `task.escalated_to_operator` event lands
  with `reason='gate-broken'` and the gate's stderr in the payload.
- Each PR runs the full `services/api` test suite (1125+ passing
  baseline) without regression.

## Constraints / scope

### In scope

- The three tasks listed below as `sequence_of_work` entries.
- AGENT.md updates per ADR-0030 for each touched component.

### Out of scope

- Frontend dashboard UI work (a separate task on the dashboard track
  can render the `reason` field once the API filter lands).
- The Step 6 test does NOT need to exercise a real Claude Code
  subprocess — it uses synthetic step.completed payloads.
- Migrating the existing wedged RAMJAC tasks. The other orchestrator
  is on that.

### Budget

3 worker dispatches, ~1 PR each. If any task wedges at the architect-amend
loop (cap fires before merge), surface to operator instead of letting
it grind — the ADR-0058 work itself shouldn't be the canary for
ADR-0058.

## Sequence of work

```yaml
sequence_of_work:
  - id: step-4-stderr-capture-test
    title: "ADR-0058 Step 4 — lock in validation log_excerpt → architect prompt path"
    workflow: wf-author
    intent: |
      STUDY: read `workers/agent/treadmill_agent/validation_runtime.py`
      (specifically `run_deterministic` at the `log_excerpt = combined[-4000:]`
      assembly around line 108) and the architect role's context-injection
      surface (find where `wf-architecture-resolve` builds the prompt
      input from the source step's payload — likely in
      `workers/agent/treadmill_agent/runner_dispositions/_context.py`
      or sibling). The 2026-05-27 RAMJAC incident proved the log_excerpt
      reaches the step output; what's NOT yet pinned is that it reaches
      the architect's prompt input intact.

      BUILD: write `workers/agent/tests/test_log_excerpt_pipeline.py`
      with a single test `test_step_output_log_excerpt_reaches_architect_prompt`
      that:
        (1) Constructs a `CheckResult` with a synthetic stderr containing
            `ModuleNotFoundError: No module named 'aws_cdk'` (the
            canonical 2026-05-27 case).
        (2) Wraps it in a `StepOutput` payload as the source step would.
        (3) Drives the architect's context-injection helper (whatever
            function builds the prompt input from a source step) against
            that payload.
        (4) Asserts the resulting prompt string contains the
            ModuleNotFoundError text verbatim — pin the data flow at
            the load-bearing seam so a future refactor that truncates
            or drops `log_excerpt` fails loudly.

      DOC: update `workers/agent/AGENT.md` Recent-changes with a
      one-line entry naming the new regression test and citing
      ADR-0058 Step 4 as the source.

      Validation MUST NOT use `cdk synth`, `docker`, live AWS commands,
      or anything network-dependent (the worker sandbox is hermetic).
      Use only the focused-pytest path below.
    scope:
      files:
        - workers/agent/tests/test_log_excerpt_pipeline.py
        - workers/agent/AGENT.md
      services_affected:
        - workers/agent
      out_of_scope:
        - Modifying validation_runtime.py itself
        - Modifying the architect's context-injection code (this task is read-only against production code)
    validation:
      - kind: deterministic
        description: New regression test passes; existing dispositions suite stays green.
        script: |
          cd workers/agent && uv run pytest tests/test_log_excerpt_pipeline.py tests/test_runner_dispositions.py -q
        severity: blocking
        timeout_seconds: 120
      - kind: llm-judge
        description: AGENT.md Recent-changes carries the Step 4 entry per ADR-0030.
        prompt: |
          The DIFF should include a Recent-changes entry in workers/agent/AGENT.md
          naming the new test file and citing ADR-0058 Step 4. Return verdict 'pass'
          when both are present; 'fail' otherwise.
        severity: blocking

  - id: step-5-overview-reason-filter
    title: "ADR-0058 Step 5 — dashboard /overview ?reason= filter for escalations"
    workflow: wf-author
    intent: |
      STUDY: read `services/api/treadmill_api/routers/dashboard/overview.py`
      (specifically `_ESCALATIONS_SQL` and the route handler) and the
      `TaskEscalatedToOperator` payload at
      `services/api/treadmill_api/events/task.py` (the `reason` field
      added in ADR-0058 Step 3, PR #53). Existing escalation rows
      carry a `reason` value in the event payload; the overview
      endpoint reads them but doesn't currently filter on it.

      BUILD: add an optional `reason` query parameter to
      `GET /api/v1/dashboard/overview` accepting one of
      `architect_cap` | `stuck_task_sweep` | `gate-broken`. When set,
      filter the `escalations` array (server-side, in the SQL or in
      the post-query filter — match the existing pattern for `repo` /
      `bucket` / `account` params) so only rows whose escalation
      event's `payload.reason` matches are returned. Surface the
      `reason` field on each escalation entry in the response payload
      so the frontend can render per-reason badges later (separate
      task on the dashboard track).

      DOC: update the overview's key-surfaces line in
      `services/api/AGENT.md` to mention the new filter; add a
      Recent-changes entry citing ADR-0058 Step 5.

      Validation MUST NOT use `cdk synth`, `docker`, live AWS, or
      network egress. Focused pytest against the new + existing
      overview tests only.
    scope:
      files:
        - services/api/treadmill_api/routers/dashboard/overview.py
        - services/api/tests/test_routers_dashboard_overview.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - Frontend changes to render the new field (separate dashboard track)
        - Modifying the TaskEscalatedToOperator payload (already shipped in #53)
    validation:
      - kind: deterministic
        description: Overview tests including the new ?reason= filter coverage pass.
        script: |
          cd services/api && uv run pytest tests/test_routers_dashboard_overview.py -q
        severity: blocking
        timeout_seconds: 120
      - kind: llm-judge
        description: AGENT.md updated per ADR-0030.
        prompt: |
          The DIFF should include an AGENT.md update in services/api/AGENT.md
          either in the overview's key-surfaces line or as a Recent-changes entry
          citing ADR-0058 Step 5. Return verdict 'pass' when present; 'fail' otherwise.
        severity: blocking

  - id: step-6-integration-smoke
    title: "ADR-0058 Step 6 — integration smoke: gate-broken verdict → escalation event"
    workflow: wf-author
    intent: |
      STUDY: read `services/api/tests/test_supersede_trigger.py` (the
      pattern for testing architect-verdict triggers against a stub
      session) and the new
      `services/api/tests/test_gate_broken_trigger.py` from ADR-0058
      Step 3 (PR #53). Step 3 ships unit-level coverage of the
      trigger; Step 6 adds the end-to-end integration that the
      consumer routes a real-shaped `step.completed` to the trigger
      and the resulting event lands with the right payload shape.

      BUILD: add `services/api/tests/test_gate_broken_end_to_end.py`
      with a single test `test_architect_gate_broken_verdict_emits_escalation_event`
      that:
        (1) Builds a `StepCompleted` event with
            `payload.verdict='gate-broken'` +
            `payload.gate_log_excerpt='...ModuleNotFoundError...'`
            shaped exactly like the architect role would emit.
        (2) Stubs the session's execute results so the trigger's
            workflow_id lookup returns 'wf-architecture-resolve' and
            the task lookup returns a real-shaped Task row.
        (3) Drives `CoordinationConsumer._maybe_dispatch_gate_broken_escalation`
            directly (mirrors the supersede unit-test pattern) and
            asserts the dispatcher's `persist_and_publish` fired with
            a `TaskEscalatedToOperator` payload carrying
            `reason='gate-broken'` + the excerpt + the architect's
            run ids.
        (4) Confirms the test does NOT depend on a real Postgres or
            real dispatcher — pure unit shape, fast (<1s).

      DOC: add a Recent-changes entry in `services/api/AGENT.md`
      citing ADR-0058 Step 6.

      Validation MUST NOT use `cdk synth`, `docker`, live AWS, or
      network egress. The test itself must be sandbox-hermetic.
    scope:
      files:
        - services/api/tests/test_gate_broken_end_to_end.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - Modifying the gate-broken trigger (already shipped in #53)
        - Modifying the consumer wiring (already shipped in #53)
    validation:
      - kind: deterministic
        description: New e2e test passes; existing gate-broken + supersede unit tests stay green.
        script: |
          cd services/api && uv run pytest tests/test_gate_broken_end_to_end.py tests/test_gate_broken_trigger.py tests/test_supersede_trigger.py -q
        severity: blocking
        timeout_seconds: 120
      - kind: llm-judge
        description: AGENT.md Recent-changes carries the Step 6 entry per ADR-0030.
        prompt: |
          The DIFF should include a Recent-changes entry in services/api/AGENT.md
          citing ADR-0058 Step 6 + naming the new test file. Return verdict 'pass'
          when both are present; 'fail' otherwise.
        severity: blocking
```

## Risks / unknowns

- **Architect context-injection seam unknown.** Step 4's task assumes
  the prompt builder lives in `runner_dispositions/_context.py` or a
  sibling. If it lives elsewhere, the worker still has read-only
  scope over the workspace and can discover it. The task's scope.files
  intentionally doesn't list it (the task isn't modifying production
  code).
- **Test runtime budgets.** Each task's validation gate runs a focused
  pytest path with `timeout_seconds: 120` — generous for the worker
  sandbox but bounded so a runaway test doesn't burn worker time.
- **Concurrent-orchestrator interference.** `auto_merge: false` means
  each PR waits for human merge. The other orchestrator's RAMJAC work
  shouldn't touch any of the three task scope.files, but verify before
  merging.

## Diagram

Skipped — this plan is purely organizational (dispatching three
independent unit tasks). The ADR-0058 plan from 2026-05-27 carries the
overall sequence-diagram if one is wanted.

## Decisions captured during execution

(empty)

## Post-mortem

(filled in on completion / abandonment)
