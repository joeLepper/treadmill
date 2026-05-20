---
date: 2026-05-20
status: in-progress
related: ADR-0011, ADR-0013, ADR-0031, ADR-0048
---

# Plan: task_status precedence fix + silent dead-end backstop + diagram reconcile

## Context

A 2026-05-19 board cleanup (139 tasks → 1) surfaced two structural issues and one
documentation drift, all confirmed by subagent verification against the code:

1. **`task_status` mislabels merged tasks.** The view's `review_passed` clause
   (`0017_task_status_surface_decision_fail.py`, clause 6b) fires for any task
   that has a `github/pr_merged` event AND whose latest run is `wf-review`. That
   is the *happy path* (clean review → auto-merge, which creates no later run),
   so every smooth merge is labeled `review_passed` forever instead of
   `pr_merged`. `review_passed` is reachable only post-merge today — the inverse
   of its intended bunkhouse meaning (ADR-0013/0031: "review passed, awaiting
   merge"). The clutter regrows on every happy-path merge.

2. **Silent dead-ends.** A trigger audit found terminal states that neither
   dispatch onward nor surface to an operator. Notably the arch-resolve cap
   surfaces `escalated_to_operator` from only 2 of its 4 cap sites; ci-fix /
   conflict / doc-amend caps and give-ups have no operator surface at all
   (their catalog "surface to operator" remediation is aspirational, not wired).

3. **PR #196 diagrams are stale.** Authored 2026-05-19 but describe a
   pre-#179/#181/#184/#187/#189/#198 world: they call already-shipped work
   "proposed/new/today-broken" and reference ADR-0049 (merged as ADR-0048).

## Goal / success criteria

- A merged task derives `pr_merged` regardless of which workflow ran last;
  `review_passed` becomes a genuine **pre-merge** state (PR open, review
  `decision='approved'`, not yet merged).
- Every non-mergeable terminal either dispatches a productive next workflow or
  emits an operator-visible `escalated_to_operator` signal. No silent no-ops.
- PR #196 ships diagrams that match the merged system; ADR numbers, decision
  strings, file:line refs, and shipped-vs-proposed status all correct.
- The diagrams-lag-code learning is captured.

## Scope

**In:**
- New alembic migration: reorder `task_status` so `pr_merged` precedes
  `review_passed`; redefine `review_passed` as pre-merge approved. + test.
- Operator-surface backstop for the audit's SDE-1..6 (uniform
  `escalated_to_operator` for cap/give-up terminals; route the no-PR feedback
  terminal to architect-on-plan). + tests.
- Reconcile PR #196 diagrams to current reality + the new backstop.
- `/learning`: design diagrams can lag merged code.

**Out (deferred):**
- o11y "bots" wave — starts after this batch merges.
- Non-Treadmill-repo bootstrap — parallel track, separate planning.
- A `task_mergeability.changed`-event auto-merge trigger (ADR-worthy redesign;
  the audit's "load-bearing" long-term fix — not needed for this batch).

## Sequence

1. **PR A** — `task_status` precedence migration + test. (Foundational, isolated.)
2. **PR B** — silent dead-end operator-surface backstop + tests.
3. **PR #196** — diagram reconcile (reflects A + B) + learning doc.
4. Run API test suite; open PRs; Joe merges (single-PAT self-review gap, #108).

## Risks

- **Second alembic head.** Mitigated: confirmed single head `20260519_1930`;
  new migration chains off it. (The open `alembic heads` CI gate task exists to
  catch exactly this — keep it in mind.)
- **`review_passed` semantic change.** Audited: zero consumers outside one test;
  redefinition is safe.
- **Over-broad architect invocation (SDE-1).** Routing the no-PR feedback
  terminal to architect-on-plan widens *invocation*, not verdict power — aligned
  with ADR-0048 and not the loosening risk in the 2026-05-19 override-power
  learning.
