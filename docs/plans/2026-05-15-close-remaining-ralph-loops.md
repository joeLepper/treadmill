---
status: drafting
trigger: ADR-0038 + ADR-0039 accepted same day. Implements deadlock arbitration + verdict='error' non-gating, plus the tactical validator robustness improvements surfaced by PR #83 + PR #84.
---

# Plan: Close the remaining ralph loops (ADR-0038, ADR-0039, validator robustness)

- **Status:** drafting
- **Date:** 2026-05-15
- **Related ADRs:** ADR-0036 (parent), ADR-0037 (sibling — author-side feedback, implementation already landed in PR #84), ADR-0038 (deadlock arbitration), ADR-0039 (validator errors don't gate), ADR-0029 Q29.f (severity), ADR-0013 (mergeability VIEW)

## Goal

Implement the last two amendments to ADR-0036 plus the two tactical validator fixes that closed the operator-merge cycles in PR #83 + PR #84. After this lands, a trivial code-touching bot PR should reach `mergeable` and auto-merge without operator intervention.

## Success criteria

- A trivial single-task plan submits via API, the author authors, both wf-review and wf-validate fire, and the PR auto-merges within the 30s cool-off window — zero operator intervention.
- A wf-feedback step that completes with `decision='responded-without-change'` while the underlying review is `changes_requested` dispatches `wf-architecture-resolve` exactly once (per the new dedup namespace).
- The `task_mergeability` VIEW's `validate_decision` aggregate maps to `'fail'` only when at least one check has `verdict='fail' AND severity='blocking'` — `verdict='error'` no longer propagates.
- The LLM-judge timeout is 120s (was 60s); the `python-tests-resolve` rule script runs cleanly in the worker environment (was failing despite local pass).

## Constraints / scope

### In scope
- Four tasks below.
- One alembic migration (VIEW rewrite to drop `error` from the gate predicate).
- One ADR-0038 dispatcher path: deadlock detection in consumer + architect verdict handler producing the `review.override` synthetic event.
- The two tactical fixes: timeout bump + pytest-collect.sh worker-env fix.
- End-to-end smoke handoff doc capturing the auto-merge.

### Out of scope
- The ADR-0020 observability dashboard for per-rule error rate (mentioned as ADR-0039 follow-up; depends on the o11y stack landing).
- Retry-on-error in LLM-judge calls (ADR-0039 follow-up; complementary to the gating change).
- Rule-script exit-code convention to let deterministic rules emit `error` (ADR-0039 follow-up).
- Architect prompt tuning to bias verdict distribution (ADR-0038 follow-up; first watch the verdict distribution before tuning).
- Backfilling old workflow_run_steps to re-aggregate.

### Budget
One operator session for review + dispatch + smoke. **NOT dispatched until after operator-merge of this plan's last task or until the in-flight convergence proof completes.**

## Diagram

The ADR-0038 sequence diagram (architect arbitration cycle) is the authoritative reference for the deadlock-handling task. ADR-0039 is policy-only (no new actors); see ADR-0036's diagram for the gate composition.

## Sequence of work

```yaml
sequence_of_work:
  - id: deadlock-arbitration-dispatcher
    title: wf-feedback.responded-without-change → wf-architecture-resolve (ADR-0038)
    workflow: wf-author
    intent: |
      Per ADR-0038: when wf-feedback completes with
      ``decision='responded-without-change'`` and the underlying
      task's most-recent wf-review.decision is
      ``changes_requested``, dispatch wf-architecture-resolve
      against the same task carrying the review's rationale +
      the feedback's rationale.

      Architect's verdict (per ADR-0032 ArchitectVerdict) drives
      the next move:

        * ``accept-as-is`` → emit a ``review.override`` synthetic
          event so the mergeability VIEW projects
          ``review_decision='approved'`` for the task.
          Auto-merge follows under ADR-0031.
        * ``amend`` → dispatch ``wf-plan`` for remediation.
        * ``supersede`` → dispatch ``wf-doc-amend`` against the plan.
        * ``uncertain`` → surface to operator (capped at 5
          attempts per ADR-0029 Q29.e).

      Implementation:
        1. Extend ``coordination/consumer._maybe_fire_review_feedback``
           (or add ``_maybe_fire_deadlock_arbitration``) to
           detect the deadlock signal and call the new helper
           ``maybe_dispatch_arbitration_on_deadlock``.
        2. New ``coordination/triggers.maybe_dispatch_arbitration_on_deadlock``
           creates a wf-architecture-resolve run + step.ready;
           dedup namespace
           ``wf-architecture-resolve:<repo>:deadlock-feedback-run=<run_id>``.
        3. Define the ``review.override`` event in
           ``events/review.py`` (or extend an existing module);
           register in events/registry.py.
        4. Extend the architecture disposition (existing
           ``runner_dispositions/architecture.py``) so
           ``accept-as-is`` verdicts emit the
           ``review.override`` event.
        5. Update the mergeability VIEW's review LATERAL to
           treat the latest ``review.override`` at HEAD as
           ``decision='approved'`` if present, falling through
           to the wf-review step otherwise.
        6. Tests in
           ``services/api/tests/test_integration_event_triggers.py``:
             - the deadlock signal fires arbitration exactly once
             - dedup blocks redelivery of the same step.completed
             - cap at 5 arbitration runs per task
        7. Tests in
           ``services/api/tests/test_integration_task_mergeability.py``:
             - ``review.override`` at HEAD flips
               ``review_decision`` to ``'approved'`` regardless
               of the most-recent wf-review verdict.
    scope:
      files:
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/dispatch_dedup.py
        - services/api/treadmill_api/events/review.py
        - services/api/treadmill_api/events/registry.py
        - services/api/alembic/versions/0014_mergeability_review_override.py
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
        - services/api/tests/test_integration_event_triggers.py
        - services/api/tests/test_integration_task_mergeability.py
    validation:
      - kind: deterministic
        description: |
          Trigger + dispatcher + VIEW migration + tests in place.
        script: |
          ( cd services/api && uv run pytest tests/test_integration_event_triggers.py tests/test_integration_task_mergeability.py -q ) \
            && grep -q "deadlock-feedback-run\|maybe_dispatch_arbitration_on_deadlock" services/api/treadmill_api/coordination/triggers.py \
            && test -f services/api/alembic/versions/0014_mergeability_review_override.py

  - id: error-verdict-non-gating
    title: Mergeability + wf-validate aggregate ignore verdict='error' (ADR-0039)
    workflow: wf-author
    intent: |
      Per ADR-0039: ``verdict='error'`` from any check does not
      gate merge. The aggregate predicate narrows from
      ``(verdict IN ('fail','error') AND severity='blocking')``
      to ``(verdict='fail' AND severity='blocking')``.
      Errors are logged with rule_id + reason for ADR-0020
      observability.

      Two surfaces:

        1. ``alembic/versions/0015_mergeability_error_non_gating.py``
           rewrites the validate LATERAL in
           ``task_mergeability`` to filter the per-check
           subquery on ``(verdict='fail' AND severity='blocking')``
           only. The 'error' path stops propagating to
           ``validate_decision``.
        2. ``workers/agent/treadmill_agent/runner_dispositions/validation.py``
           — the worker's aggregate over checks now emits
           ``decision='pass'`` when no blocking check has
           ``verdict='fail'`` (errors are surfaced in the
           output payload but don't flip the aggregate). The
           aggregate is symmetric with the VIEW so projections
           agree.
        3. A structured log line per errored check —
           ``logger.warning("rule.error", extra={rule_id, reason})``
           — so the o11y stack can build a per-rule error-rate
           panel later.

      Tests:
        - VIEW integration test: a blocking check with
          ``verdict='error'`` no longer flips the aggregate
          to ``'fail'``.
        - VIEW integration test: a blocking check with
          ``verdict='fail'`` still flips the aggregate to
          ``'fail'``.
        - Worker unit test: the aggregate disposition emits
          ``decision='pass'`` when only errors are present.
    scope:
      files:
        - services/api/alembic/versions/0015_mergeability_error_non_gating.py
        - workers/agent/treadmill_agent/runner_dispositions/validation.py
        - services/api/tests/test_integration_task_mergeability.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          Migration + aggregate change + tests; error verdicts no longer aggregate.
        script: |
          test -f services/api/alembic/versions/0015_mergeability_error_non_gating.py \
            && ( cd workers/agent && uv run pytest tests/test_runner_dispositions.py -k "test_validation_handler" -q )

  - id: validator-runtime-robustness
    title: Bump LLM-judge timeout + fix pytest-collect worker-env
    workflow: wf-author
    intent: |
      Two tactical fixes complementary to ADR-0039:

      1. ``workers/agent/treadmill_agent/validation_runtime.py``:
         bump the LLM-judge per-check timeout from 60s to 120s.
         60s was tight for haiku on judge prompts against
         non-trivial diffs; 120s removes the common transient
         timeout that we observed on PR #84's
         ``implementation-conforms`` check.

      2. ``tools/rule-checks/python-tests-resolve/pytest-collect.sh``:
         the script currently returns non-zero in the worker
         environment even when ``uv run pytest --collect-only``
         succeeds locally (863 tests). Investigate (likely a cwd
         or PYTHONPATH issue) and fix. If the underlying issue
         is fundamentally environmental, the script should
         distinguish "couldn't run" (exit ≥ 2) from "collection
         failed" (exit 1) so ADR-0039's policy applies
         correctly via the script-exit convention.

      Tests:
        - validation_runtime test stub that uses the new
          timeout default.
        - if the pytest-collect fix is a script change, a
          simple smoke that runs it from a fresh shell against
          this repo and asserts exit 0.
    scope:
      files:
        - workers/agent/treadmill_agent/validation_runtime.py
        - workers/agent/tests/test_validation_runtime.py
        - tools/rule-checks/python-tests-resolve/pytest-collect.sh
    validation:
      - kind: deterministic
        description: |
          Tests pass; timeout bumped; pytest-collect runs cleanly.
        script: |
          ( cd workers/agent && uv run pytest tests/test_validation_runtime.py -q ) \
            && grep -q "120" workers/agent/treadmill_agent/validation_runtime.py \
            && bash tools/rule-checks/python-tests-resolve/pytest-collect.sh

  - id: closing-smoke
    title: End-to-end smoke proves hands-free auto-merge with zero operator action
    workflow: wf-validate
    depends_on:
      - task.deadlock-arbitration-dispatcher.pr_merged
      - task.error-verdict-non-gating.pr_merged
      - task.validator-runtime-robustness.pr_merged
    intent: |
      Submit a fresh single-task plan that touches a trivial
      file (e.g., one line appended to
      ``docs/handoffs/2026-05-15-hands-free-final-smoke.md``).

      Observe:
        - wf-author opens the PR (synthesized 5-section body
          per ADR-0033 + #76)
        - wf-review approves (no reviewer-vs-feedback deadlock,
          because if it happened ADR-0038 would resolve it
          via architect)
        - wf-validate runs only applicable rules; any LLM-judge
          errors do not gate (ADR-0039)
        - mergeability=mergeable; 30s cool-off elapses;
          auto-merge fires
        - PR state=MERGED with zero operator-merge intervention

      Document in
      ``docs/handoffs/2026-05-15-hands-free-final-smoke.md``
      with cycle counts, wall-clock latency, and any verdicts
      observed.
    scope:
      files:
        - docs/handoffs/2026-05-15-hands-free-final-smoke.md
    validation:
      - kind: deterministic
        description: |
          Handoff doc exists; cites auto-merge firing.
        script: |
          test -f docs/handoffs/2026-05-15-hands-free-final-smoke.md \
            && grep -qi "auto.merge.fired\|MERGED.*no.operator" docs/handoffs/2026-05-15-hands-free-final-smoke.md
```

## Risks / unknowns

- **The architect's verdict distribution.** ADR-0038 assumes role-architect produces useful `accept-as-is` verdicts. If haiku-as-architect leans heavily on `uncertain`, the operator surface fills up. Mitigation: instrument the verdict counter; if `uncertain` rate exceeds N%, bump the role to a more capable model.
- **VIEW migration drift.** Two alembic migrations (0014, 0015) within the same plan touching the same VIEW. Migration 0015 must be authored against the 0014 shape, not 0013's. Mitigation: serialize the tasks (0014 lands first, 0015 builds on it); both migrations include their own up/down so we can roll forward or back cleanly.
- **pytest-collect worker-env divergence.** We don't yet know why the script fails in the worker but succeeds locally. The fix may turn out to be a worker-image change rather than a script change. Mitigation: if the fix touches the worker image, expect a docker rebuild + redeploy as part of the task; the task can absorb that scope.
- **Trivial smoke may still need operator-merge.** Even with all gates relaxed, the reviewer may keep emitting `changes_requested`. The ADR-0038 arbitration path is what bypasses that; if it dispatches but architect also disagrees, we surface to operator. We abort + write a post-mortem rather than escalate quietly.

## Decisions captured during execution

(empty)

## Post-mortem

(empty)
