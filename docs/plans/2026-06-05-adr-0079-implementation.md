# Plan: ADR-0079 implementation — dispatcher short-circuit on terminal task status

- **Status:** drafting
- **Date:** 2026-06-05
- **Related ADRs:** ADR-0079 (the decision, PR #225), ADR-0062 (terminal_step_failure escalation producer), ADR-0029 (architect amend cap), ADR-0030 (rule-check pattern)

## Goal

Land the dispatcher-side guard that short-circuits `step.ready`
assignment when the owning task is in a terminal status
(`pr_merged`, `cancelled`, `superseded`,
`escalation_closed_terminal`) AND the workflow type is
action-class (the wf-author family). Add the new
`step.skipped` event action so the skip is observable in events,
the dashboard, and downstream sweeps.

Closes the post-merge empty-diff escalation loop measured at
~15 sessions / ~86K output tokens / ~$1.30 on the ADR-0076
implementation alone (PRs A and B).

## Success criteria

1. New `step.skipped` event payload in
   `services/api/treadmill_api/events/step.py` and registered
   in `events/registry.py`. Carries `task_id`, `step_id`,
   `reason: Literal["task_terminal"]`, `terminal_status: str`.
2. Dispatcher seam (in `coordination/consumer.py` or wherever
   `step.ready` becomes a worker assignment — STUDY pins the
   exact line) reads the owning task's `status` before assigning.
   When the status is terminal AND the workflow type is
   `wf-author` / `wf-feedback` / `wf-architecture-resolve`
   (the action-class set), the consumer:
   - Updates the step row to `status='skipped'`,
     `skip_reason='task_terminal'`.
   - Acks/deletes the SQS message (no redeliver).
   - Emits the `step.skipped` event via
     `dispatcher.persist_and_publish`.
3. Read-only workflow types (`wf-validate`, `wf-review`,
   `wf-analyze`) on terminal tasks are NOT short-circuited.
   Logged as a guarded comment in the dispatcher seam so the
   narrowing is visible.
4. The
   `maybe_dispatch_terminal_step_failure_escalation` trigger
   (ADR-0062 Step 1) is NOT modified — it still fires on
   `step.failed` legitimately produced by non-terminal tasks.
   The skip path doesn't go through `step.failed`, so the
   trigger is dead-code on the new path; that's correct.
5. End-to-end smoke test against a synthetic
   `step.ready` whose owning task is `pr_merged`: the
   consumer emits `step.skipped`, no `step.failed`, no
   escalation. Test lives at
   `services/api/tests/test_dispatcher_short_circuit_terminal_task.py`.
6. `services/api/AGENT.md` Recent-changes entry citing
   ADR-0079.

## Constraints / scope

### In scope

- New `StepSkipped` payload in `events/step.py` + registry
  registration.
- Dispatcher-seam edit in `coordination/consumer.py` (or
  sibling — STUDY confirms).
- Action-class workflow type set defined as a single module-
  level constant so it's discoverable and extensible (mirrors
  how `ARCHITECTURE_RESOLVE_WORKFLOW_ID` is named in
  `coordination/triggers.py`).
- Unit + smoke tests at the appropriate test files.
- `services/api/AGENT.md` Recent-changes entry per ADR-0030.

### Out of scope

- **Backfill of currently-looping tasks** (ADR-0076 PR A +
  PR B are still firing escalations). Operator runs a one-shot
  SQL post-merge to mark open workflow_runs terminated; not
  this PR's scope.
- **Dashboard surface for `step.skipped`** — operator can
  query the event today; pretty rendering is a separate
  dashboard-track follow-up.
- **Widening the guard to read-only workflow types**
  (wf-validate / wf-review / wf-analyze). Doc the narrowing;
  widen if observation justifies. Out of v1.
- **Alembic migration** — both `events.action` and
  `workflow_run_steps.status` are unconstrained varchar
  columns (verified 2026-06-05); no schema change needed.

### Budget

One PR, hand-authored OR worker-dispatched. Estimated
~half-day. No `auto_merge: false` warranted — no shared
schema, no Alembic, no CDK changes.

## Sequence of work

```yaml
sequence_of_work:
  - id: dispatcher-short-circuit
    title: "ADR-0079 — dispatcher short-circuits step.ready on terminal task status"
    workflow: wf-author
    intent: |
      STUDY:
        - docs/adrs/0079-dispatcher-short-circuits-step-ready-on-terminal-task-status.md
          — the decision; the action-class workflow narrowing and the
          step.skipped event shape are load-bearing.
        - services/api/treadmill_api/coordination/consumer.py —
          find the seam where `step.ready` becomes a worker
          assignment. The SQS poll + `_handle_step_ready` path
          is the likely site; confirm via grep before editing.
        - services/api/treadmill_api/events/step.py — existing
          payloads (StepReady, StepStarted, StepCompleted,
          StepFailed). Mirror their shape for StepSkipped.
        - services/api/treadmill_api/events/registry.py — how
          events register so the new payload picks up the
          consumer's projection.
        - services/api/treadmill_api/coordination/triggers.py —
          read `ARCHITECTURE_RESOLVE_WORKFLOW_ID` constant for
          naming convention; mirror it as
          `ACTION_CLASS_WORKFLOW_IDS = frozenset({...})`.
        - services/api/treadmill_api/models/task.py (or
          equivalent) — confirm the terminal status set:
          `pr_merged | cancelled | superseded | escalation_closed_terminal`.

      BUILD:
        1. New `StepSkipped` payload in events/step.py:
             - ENTITY_TYPE = "step"
             - ACTION = "skipped"
             - Fields: task_id (UUID), step_id (UUID),
               reason: Literal["task_terminal"], terminal_status: str
        2. Register in events/registry.py alongside the
           existing step payloads.
        3. Module-level constant in
           coordination/consumer.py (or sibling):
             ACTION_CLASS_WORKFLOW_IDS = frozenset({
               WF_AUTHOR_ID, WF_FEEDBACK_ID,
               WF_ARCHITECTURE_RESOLVE_ID,
             })
           Source the literal workflow_ids from the same
           place triggers.py reads them.
        4. In the step.ready handler (the dispatcher seam),
           BEFORE assigning to a worker:
             - SELECT task.status from the owning task row
               (already in session for related lookups; one
               extra query in the worst case).
             - If status IN TERMINAL_TASK_STATUSES AND
               workflow_id IN ACTION_CLASS_WORKFLOW_IDS:
                 * UPDATE workflow_run_steps SET
                   status='skipped', skip_reason='task_terminal'
                   WHERE id=step_id.
                 * Emit StepSkipped via
                   dispatcher.persist_and_publish.
                 * Ack the SQS message; return without
                   assigning.
        5. Log the skip at WARNING level with task_id +
           step_id + terminal_status so operators can see the
           guard firing in production logs.

      TEST:
        - services/api/tests/test_dispatcher_short_circuit_terminal_task.py
          (new file, OR add to test_consumer_unit.py if that's
          the test file pattern for the seam):
          * test_short_circuits_on_pr_merged_status: stub a
            task with status='pr_merged' and an action-class
            workflow; drive the seam; assert step row updated
            to 'skipped', StepSkipped event emitted, no
            StepFailed.
          * test_short_circuits_on_cancelled_status: same
            with status='cancelled'.
          * test_does_not_short_circuit_on_running_status:
            non-terminal status; existing assignment path
            fires.
          * test_does_not_short_circuit_on_read_only_workflow:
            terminal task + wf-validate; existing path fires
            (read-only workflow not narrowed).
          * test_short_circuit_emits_no_step_failed_or_escalation:
            pin the silence-on-skip invariant explicitly.
        - services/api/tests/test_events.py — add a small
          round-trip for StepSkipped to pin payload shape.

      DOC: services/api/AGENT.md Recent-changes entry citing
      ADR-0079, naming the dispatcher-seam method,
      the new event payload, and the
      ACTION_CLASS_WORKFLOW_IDS constant.

      Validation MUST NOT use cdk synth, docker, live AWS,
      or network egress. Focused pytest against the touched
      test files.
    scope:
      files:
        - services/api/treadmill_api/events/step.py
        - services/api/treadmill_api/events/registry.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_dispatcher_short_circuit_terminal_task.py
        - services/api/tests/test_consumer_unit.py
        - services/api/tests/test_events.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - Backfilling currently-looping tasks
        - Dashboard rendering of step.skipped
        - Widening the guard to read-only workflow types
        - Alembic migration (no schema change required —
          events.action and workflow_run_steps.status are
          unconstrained varchar)
    validation:
      - kind: deterministic
        description: |
          Dispatcher short-circuit tests + event payload
          round-trip pass; existing consumer / triggers
          unit suites remain green.
        script: |
          cd services/api && uv run pytest tests/test_dispatcher_short_circuit_terminal_task.py tests/test_consumer_unit.py tests/test_events.py tests/test_terminal_step_failure_producer.py -q
        severity: blocking
        timeout_seconds: 180
      - kind: llm-judge
        description: |
          AGENT.md Recent-changes carries an ADR-0079 entry per
          ADR-0030.
        prompt: |
          The DIFF should include a Recent-changes entry in
          services/api/AGENT.md citing ADR-0079 and naming the
          dispatcher-seam method, the StepSkipped event, and
          the ACTION_CLASS_WORKFLOW_IDS constant. Return
          verdict 'pass' when present; 'fail' otherwise.
        severity: blocking
```

## Risks / unknowns

- **Dispatcher seam location**: the step.ready → assign path
  may live in `coordination/consumer.py` or in a sibling
  module. STUDY phase locates the exact seam before editing.
  The wf-author re-author cycles today don't bypass the seam,
  so a single hook is sufficient.
- **Race between task.status update and step.ready
  assignment**: a worker could merge a PR and the dispatcher
  could read `status='running'` if the projection lag is
  longer than the dispatcher's read transaction. Mitigation:
  read the task row in the same transaction as the step
  assign. Worst case the loop fires one extra cycle before
  the guard catches it.
- **Event projection downstream**: the
  `maybe_dispatch_terminal_step_failure_escalation` trigger
  fires on `step.failed`. The new `step.skipped` action
  shouldn't trip it. Verified by `test_short_circuit_emits_no_step_failed_or_escalation`.

## Diagram

Reference ADR-0079's sequence diagram (the operator → API +
dispatcher consultation path).

## Decisions captured during execution

_Empty — populated as we work._

## Post-mortem

_Filled on completion / abandonment._
