# Plan: ADR-0048 architect-widening — remaining implementation

Status: **active**
ADR: [ADR-0048](../adrs/0049-architect-widens-from-arbitrator-to-recoverer.md)
Diagrams: [`docs/diagrams/task-flow-*.md`](../diagrams/task-flow-overview.md) (catalog: dead-ends per PR #178)

## Why this plan exists

ADR-0048 was written with most of the foundation already shipped (PRs #179, #180, #181). What remains is wiring the new routes, smoke-testing end-to-end, and giving the operator a surface for the residual dead-end class. This plan tracks that.

Tasks are ordered so each one delivers operator-observable value before the next.

## Task 1 — Wire wf-author no-diff `step.failed` to wf-architecture-resolve

**Why first:** this is the route that unsticks the `author-no-diff` class in the dead-end catalog (11 tasks in the 2026-05-19 audit). The supersede affordance shipped in #181 is the destination; we still need the route.

**Scope:**
- In `services/api/treadmill_api/coordination/triggers.py`, distinguish wf-author `step.failed` shapes by inspecting the step's `error` text.
- For `error` starting with `"Claude Code produced no changes to commit"` (or whatever exact string the worker emits — verify by grepping the worker source), dispatch wf-architecture-resolve instead of wf-feedback.
- Keep the existing wf-feedback dispatch for other `step.failed` shapes (worker crash, author-validations rejected).
- Dedup key: `wf-architecture-resolve:<repo>:author-no-diff-run=<wf_author_run_id>`.

**Tests:**
- Unit: a wf-author `step.failed` with the `"no changes to commit"` error string dispatches wf-architecture-resolve, not wf-feedback.
- Unit: a wf-author `step.failed` with a different error string still dispatches wf-feedback.

**Done when:** a real task that previously dead-ended at `author-no-diff` now reaches `wf-architecture-resolve.step.started` in the workflow log.

## Task 2 — Wire wf-author remote-rejection `step.failed` to wf-architecture-resolve

**Why second:** remote rejections have been observed once in the audit (`pr-remote-rejected`); they're rare, but the route is small once Task 1 establishes the routing pattern.

**Scope:**
- Identify the exact error surface for remote rejection (grep `workers/agent/treadmill_agent/git.py` and surrounding for the rejection error). It will look something like `"remote rejected"` or `"failed to push some refs"`.
- Mirror Task 1's pattern: dispatch wf-architecture-resolve on this `step.failed` shape.
- Dedup key: `wf-architecture-resolve:<repo>:remote-rejected-run=<wf_author_run_id>`.

**Tests:**
- Unit: a wf-author `step.failed` with the remote-rejection error string dispatches wf-architecture-resolve.
- Unit: covers the case where the architect emits `supersede` against this trigger source (architect's input includes the failed push, which it should recognize as needing a fresh branch).

**Done when:** a synthetic test that causes a remote-rejection produces a supersede-and-child-task flow end-to-end.

## Task 3 — End-to-end integration test for supersede

**Why third:** PR #181 covered trigger-level mechanics (close PR, create child, dispatch fresh wf-author). The integration test that's missing: an architect step.completed with verdict=supersede actually produces a child task that the worker pool then picks up, authors against, opens a PR for, and merges. This is the "supersede really works" smoke.

**Scope:**
- Add `services/api/tests/test_integration_supersede_end_to_end.py`.
- Use the existing integration harness (likely the same shape as `test_integration_task_retry.py`).
- Steps:
  1. Register a task with a deliberately-broken description.
  2. Drive a wf-author run that fails with `"no changes"` (mock the worker to short-circuit).
  3. Mock-dispatch wf-architecture-resolve and have it emit `verdict=supersede` with a rewritten_description.
  4. Assert: child task row created, parent's PR closed (if any), fresh wf-author dispatch event emitted.
  5. Drive the child's wf-author to completion (mocked to produce a real diff this time).
  6. Assert: child reaches mergeability, auto-merge fires.

**Done when:** the integration test passes in CI.

## Task 4 — Operator surface for `arch-cap-reached`

**Why fourth:** even with the architect's full escalation menu, some tasks may exhaust the 5-attempt cap on `wf-architecture-resolve`. Today these silently terminate. We want operator notification before the run-of-the-mill audit catches them hours later.

**Scope:**
- Emit a structured `task.escalated_to_operator` event (or similar; check existing event naming conventions) when a wf-architecture-resolve dispatch would have fired but the cap blocks it.
- Event payload: task id, repo, last architect verdict, last architect reasoning, link to most recent runs.
- Surface options (pick one — call it out in the PR description):
  - PR comment on the parent task's PR (if open)
  - SNS topic that an external notifier (Slack, email, dashboard) can subscribe to
  - Just a structured log + a `/api/v1/tasks?status=needs_operator` query parameter the operator polls

**Tests:**
- Unit: cap-hit dispatch path emits the event and does NOT silently no-op.

**Done when:** the operator can identify cap-hit tasks without a full audit query.

## Task 5 — Diagram cleanup after PR #178 lands

**Why last:** the dead-end catalog was updated in PR #178 to match the post-ADR-0048 architecture. Once #178 merges, the catalog references `arch-uncertain-surfaced` and `supersede-not-implemented` as removed; the implementation has shipped them. A small cleanup pass:

- Remove the "DEAD-END today:" annotations from the supersede + uncertain rows in the per-workflow diagrams; they're no longer terminal.
- Update the "post-0049" sections in the diagrams to mark which routes are now wired (Task 1, 2) vs still pending.
- Refresh the count column in the catalog with a fresh audit (post-Task-1, the `author-no-diff` count should drop).

**Done when:** the diagrams reflect main accurately.

## Out of scope

- **The integration-test gap on the existing architect-verdict dispositions** (review.override, validate.override). Those have been working in production; their integration coverage is pre-existing tech debt independent of this plan.
- **Operator-initiated supersede CLI** — a `treadmill task supersede <task-id> --rewrite "<new text>"` CLI surface would let the operator do manually what the architect does automatically. Worth its own ADR; not required for this plan.
- **Per-task supersede cap separate from arch-cap.** Captured as an [open question in ADR-0048](../adrs/0049-architect-widens-from-arbitrator-to-recoverer.md#open-questions-for-follow-up-adrs).
- **Lineage depth limits.** Same — open question in the ADR; out of scope here.

## Success criteria for the plan as a whole

The plan is done when:

1. The `author-no-diff` class in the dead-end catalog drops to 0 — tasks that previously dead-ended now reach the architect.
2. A synthetic supersede test passes end-to-end in CI.
3. The operator has a notification surface (no more audit-after-the-fact for cap-hits).
4. The diagrams reflect main accurately.

If a real task autonomously flows `wf-author → architect → supersede → child task → wf-author → wf-validate → wf-review → wf-merged`, that's the strongest possible signal. Mark the plan complete when we observe one.

## sequence_of_work

```yaml
sequence_of_work:
  - id: route-author-no-diff-to-architect
    title: Route wf-author no-diff step.failed to wf-architecture-resolve
    workflow: wf-author
    intent: |
      The wf-author worker raises ``CodeAuthorError("Claude Code
      produced no changes to commit")`` when it can't produce a diff
      against the task spec (per
      ``workers/agent/treadmill_agent/runner_dispositions/code.py``).
      Today this surfaces as a ``step.failed`` and dispatches
      wf-feedback via
      ``maybe_dispatch_feedback_on_step_failed`` in
      ``services/api/treadmill_api/coordination/triggers.py``.

      This is wrong-shaped per ADR-0048 — wf-feedback looks at a
      gate-bearing failure on an existing PR, and an author no-diff
      means no PR was ever opened, so feedback has nothing to
      remediate. The correct route is wf-architecture-resolve, where
      the architect reads the task spec + branch state and emits one
      of amend (iterate with hint), supersede (rewrite task text +
      restart fresh), or accept-as-is (work is genuinely done
      elsewhere).

      Implementation:
        - In ``triggers.py``, when
          ``maybe_dispatch_feedback_on_step_failed`` would fire on a
          wf-author step.failed, first inspect the step's ``error``
          text. If the error contains the no-changes signature (grep
          ``workers/agent/`` for the exact string), instead dispatch
          ``wf-architecture-resolve`` against the same task.
        - Dedup key:
          ``wf-architecture-resolve:<repo>:author-no-diff-run=<wf_author_run_id>``.
        - Keep the wf-feedback dispatch for all other step.failed
          shapes (worker crash, author-validations rejected).
        - Add a sibling ``maybe_dispatch_architect_on_author_no_diff``
          helper following the same pattern as the other
          ``maybe_dispatch_*`` helpers — predicate, dedup, dispatch.

      Tests in
      ``services/api/tests/test_triggers_author_no_diff_routing.py``
      (new file):
        - A wf-author ``step.failed`` with the no-changes error
          dispatches wf-architecture-resolve, NOT wf-feedback.
        - A wf-author ``step.failed`` with any other error still
          dispatches wf-feedback (regression for the existing path).
        - Dedup is keyed on the wf-author run id, not the step id.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_triggers_author_no_diff_routing.py
    validation:
      - kind: deterministic
        description: |
          New trigger function dispatches wf-architecture-resolve on
          the no-diff error; wf-feedback dispatch is suppressed on the
          same step. Both regression tests pass.

  - id: route-author-remote-rejection-to-architect
    title: Route wf-author remote-rejection step.failed to wf-architecture-resolve
    workflow: wf-author
    depends_on:
      - route-author-no-diff-to-architect
    intent: |
      Mirror the routing established for author-no-diff (task 1) but
      for the "remote rejected push" failure shape. When the wf-author
      worker's ``git push`` is rejected by GitHub (branch protection,
      conflicting state, etc.), today it surfaces as ``step.failed``
      indistinguishable from any other crash. Per ADR-0048 this should
      route to wf-architecture-resolve so the architect can decide —
      almost always with ``supersede`` to close the rejected branch
      and start fresh.

      Identify the exact error surface for remote rejection — grep
      ``workers/agent/treadmill_agent/git.py`` and surrounding for the
      rejection error pattern (probably ``"remote rejected"`` or
      ``"failed to push some refs"``). Once identified, extend the
      step.failed routing from task 1 to also recognize this shape.

      Dedup key:
      ``wf-architecture-resolve:<repo>:remote-rejected-run=<wf_author_run_id>``.

      Tests in
      ``services/api/tests/test_triggers_author_no_diff_routing.py``
      (same file as task 1):
        - A wf-author ``step.failed`` with the remote-rejection error
          string dispatches wf-architecture-resolve.
        - The routing distinguishes remote-rejection from no-diff
          (different dedup namespaces).
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_triggers_author_no_diff_routing.py
    validation:
      - kind: deterministic
        description: |
          Remote-rejection step.failed dispatches
          wf-architecture-resolve under its own dedup namespace.
          Regression tests pass.

  - id: integration-test-supersede-end-to-end
    title: End-to-end integration test for supersede affordance
    workflow: wf-author
    depends_on:
      - route-author-no-diff-to-architect
    intent: |
      Add an integration test that drives a full supersede flow end-
      to-end: a wf-author run produces no diff (mocked), the
      architect dispatches with verdict=supersede + a
      rewritten_description, a child task is created with
      parent_task_id pointing back, a fresh wf-author dispatches
      against the child (mocked to produce a real diff this time),
      and the child reaches mergeability.

      PR #181 covered the trigger-level mechanics
      (``test_supersede_trigger.py``). The gap this task closes is the
      end-to-end flow: trigger + child task creation + fresh dispatch
      + worker pickup + auto-merge.

      Pattern after
      ``services/api/tests/test_integration_task_retry.py`` — same
      shape of integration harness, same SQS+Postgres+API mocking.

      File:
      ``services/api/tests/test_integration_supersede_end_to_end.py``
      (new). Test stages:
        1. Register a task with a deliberately-broken description.
        2. Drive wf-author to fail with the no-diff error (mocked).
        3. Drive task 1's new routing — verify
           wf-architecture-resolve dispatches.
        4. Mock architect emit verdict=supersede with a
           rewritten_description.
        5. Assert: child task row created, parent's PR closed (if
           any), fresh wf-author dispatch event emitted.
        6. Drive the child's wf-author to completion (mocked to
           produce a real diff).
        7. Assert: child reaches mergeability; auto-merge fires.
    scope:
      files:
        - services/api/tests/test_integration_supersede_end_to_end.py
    validation:
      - kind: deterministic
        description: |
          The new integration test passes in CI. The test exercises
          the full supersede flow end-to-end (trigger → child task →
          fresh dispatch → mergeable).

  - id: operator-surface-for-arch-cap-reached
    title: Operator notification surface for arch-cap-reached
    workflow: wf-author
    intent: |
      When ``wf-architecture-resolve`` would dispatch but its
      per-task cap (5, per ADR-0029 Q29.e) blocks the dispatch, the
      system today silently no-ops. Per ADR-0048 §3 escalation 3,
      this should produce an operator-visible signal.

      Emit a structured event when an architect cap-block fires.
      Event name: ``task.escalated_to_operator`` (or a similar slug
      that matches conventions — check ``events/`` for the naming
      pattern). Event payload: task_id, repo, last architect verdict,
      last architect reasoning, link to the most recent runs.

      Pick ONE notification surface (call out which one in the PR
      description):
        - Option A: post a comment on the parent task's PR (if
          open).
        - Option B: emit to an SNS topic that an external notifier
          subscribes to.
        - Option C: structured log + a
          ``GET /api/v1/tasks?status=needs_operator`` query
          parameter the operator polls.

      Option C is the smallest move; A is the most operator-visible.
      Pick based on what's already plumbed; if neither A nor B is
      already wired, default to C.

      Tests in
      ``services/api/tests/test_arch_cap_operator_surface.py``
      (new file):
        - When ``_is_capped`` returns true for
          wf-architecture-resolve, the new event/notification fires
          (where it didn't before).
        - The notification carries the load-bearing fields
          (task_id, last verdict, last reasoning).
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/events/task.py
        - services/api/tests/test_arch_cap_operator_surface.py
    validation:
      - kind: deterministic
        description: |
          A cap-blocked dispatch attempt produces an operator-
          observable signal; the test asserts the signal fires with
          the expected payload shape.
```
