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
