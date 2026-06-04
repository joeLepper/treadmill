---
date: 2026-06-03
trigger: surprise
status: captured
related: ADR-0035, ADR-0057, ADR-0061
---

# Learning: scheduler should dedupe pending ticks per (workflow, schedule)

## Trigger

The `wf-ui-triage` schedule (cron `7 */4 * * *`, ADR-0061 Step 5) accumulated
**6 pending tick Tasks** under `SYSTEM_PLAN_ID` while the worker pool was
saturated by a long-running upstream task. None of the 6 had a worker-claimed
step (`workflow_run_steps.started_at IS NULL` on every row); each represented
a distinct cron fire whose synthetic Task was registered, queued, and never
consumed. When the worker pool finally drained, the backlog dispatched one
after another — six identical UI-triage runs against the same dashboard
state, producing six near-duplicate finding batches over a 90-second window.

## Observation

ADR-0057 made every scheduled tick produce a Task via the normal
`dispatch_task` path. That decision was correct (workers see a normal body
instead of the silent-failing `task_id=null` envelope), but it inherited an
implicit assumption from the user-task model: every Task represents
*intentional* user work, so we never collapse them. For scheduler-spawned
synthetic Tasks the assumption inverts — each Task represents a *moment*
(cron fire 1, cron fire 2, …) and a moment whose work hasn't started yet is
indistinguishable from the next moment's work. Six identical moments in the
queue is six wasted role-runs.

The two deterministic-detector workflows (`wf-stuck-task-sweep`,
`wf-escalation-close-sweep`) are immune: their `handle_scheduled_tick`
short-circuits run the detector in the same transaction as the tick event,
so there is no Task to queue and no backlog to form. Only the workflows that
fall through to `_dispatch_via_synthetic_task` can pile up.

## Generalization

When a primitive emits "this should happen now" events on a schedule and the
worker consumer can run slower than the schedule fires, the queue between
them needs a collapsing rule. The rule is the same shape every backpressure
system learns once: *N pending instances of the same intent ≡ 1 pending
instance*, where "same intent" is the equivalence relation the producer
controls. For Treadmill schedules the equivalence is
`(workflow_version_id, schedule_id) + not-yet-started`. For RAMJAC scrapes
it was `(scraper_id, target_url) + not-yet-fetched`. Same generalization,
different keys.

In-flight ticks (a step has `started_at IS NOT NULL`) are not part of the
equivalence class — the work has already begun, collapsing it would lose
the worker's progress. Parallel runs of the same workflow remain allowed by
design; only the *backlog* collapses.

## Proposed rule

`handle_scheduled_tick` collapses any prior pending synthetic Tasks for the
same `(workflow_version_id, plan_id=SYSTEM_PLAN_ID, created_by='scheduler')`
before emitting a fresh tick's Task. Pending = no `workflow_run_steps` row
with `started_at IS NOT NULL`. Each collapsed Task gets a `task.cancelled`
event with `reason='superseded_by_newer_tick'` + the `schedule_id` so the
audit stream distinguishes coalesce-cancellations from operator-driven ones.

## Proposed remediation

Deterministic helper `_coalesce_pending_ticks_for_schedule` in
`services/api/treadmill_api/coordination/triggers.py`, called between the
`WorkflowVersion` lookup and `_dispatch_via_synthetic_task` for every
non-deterministic-detector schedule slug. The detector slugs (which
short-circuit before `_dispatch_via_synthetic_task` ever runs) never reach
the helper. The new behavior is observable through the `task.cancelled`
event stream — no API or model change is needed.

## Notes

The 2026-06-03 backlog was visible *because* the synthetic-task path
(ADR-0057) makes scheduler-spawned work first-class in the Tasks list — the
old taskless path would have shown 6 orphan `workflow_runs` rows with no
clear lineage and the redundant role-runs would have looked like normal
fan-out. The fix sits one layer above ADR-0057: that ADR made the work
*visible*; this rule makes it *collapsible*.
