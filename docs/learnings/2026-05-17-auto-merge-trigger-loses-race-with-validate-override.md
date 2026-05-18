---
date: 2026-05-17
trigger: surprise
status: captured
related: ADR-0031, ADR-0038
---

# Learning: Auto-merge trigger loses the race with validate.override commit

## Trigger

Five PRs (#126, #127, #129, #130, #131) sat open for 6-14 hours despite the mergeability VIEW reporting `derived_mergeability=mergeable` for all of them with `review_decision=approved`, `validate_decision=pass`, `ci_conclusion=success`. The auto-merge mechanism (ADR-0031) never fired. Redis had zero `treadmill:auto-merge-deadline:*` keys and only one historical `auto-merge-fired:*` key from earlier in the day. The 5 PRs had to be merged manually with `gh pr merge --squash`.

## Observation

In `services/api/treadmill_api/coordination/consumer.py` lines 423–453, three triggers run sequentially inside one transaction on step.completed:

1. `_maybe_emit_validate_override` — inserts a `validate.override` event row
2. `_maybe_fire_architect_amend_feedback`
3. `_maybe_fire_auto_merge` — reads the mergeability VIEW, which projects from `validate.override` + `review.override` events

`session.commit()` runs at line 453, after all three. There is no `await session.flush()` between step 1 and step 3. When `_maybe_fire_auto_merge` queries the VIEW, the override row added in step 1 may not yet be visible to the SELECT — so the VIEW returns `validate_decision=NULL` or `unknown` and `maybe_auto_merge_on_mergeable` bails before writing the cooling-off deadline. No deadline → no merge → PR sits open.

The bailout is silent (no log lines). The single PR that did auto-merge today (task `a214a52f`) won that race; the four that didn't lost it.

## Generalization

When an event-driven projection (a VIEW reading from events) is consulted in the **same transaction** that just inserted an event into its source, an explicit `session.flush()` is required between the write and the read. Without it, the read may see the pre-write snapshot and silently bail. The bug compounds when the consulting code's bailout path is silent — there is no signal that the race fired.

This is the same shape as task #132 ("SQL-bypass writes to task_dependencies need a re-eval trigger") and ADR-0038's deadlock arbitration: any projection source change that should retrigger a downstream effect needs either an explicit flush + re-read, or an event-driven re-eval, not transaction-internal optimistic reads.

## Proposed rule

> When code consults a projection VIEW after inserting into the VIEW's source-events table inside the same transaction, it must `await session.flush()` between the insert and the read. Silent bailouts on `derived_*=NULL` must log at INFO level so the race becomes observable.

## Proposed remediation

Two-line fix: insert `await session.flush()` between consumer.py:426 and :441; add a single log line in `maybe_auto_merge_on_mergeable` when `derived_mergeability != 'mergeable'` so silent bailouts become visible. Both are reversible and low-blast-radius.

Durable fix lives in task #132's territory (re-eval triggers for projection-source writes).

## Notes

Confirmed by direct merge: `gh pr merge --squash` on all 5 succeeded immediately with no protection / approval / conflict obstacles, ruling out PAT permissions, branch protection, or merge conflicts as the cause. The mergeability VIEW was correct; only the trigger that consumes it failed to fire.
