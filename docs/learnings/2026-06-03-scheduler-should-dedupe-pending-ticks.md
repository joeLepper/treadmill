---
date: 2026-06-03
trigger: surprise
status: captured
related: ADR-0035, ADR-0057
---

# Learning: the scheduler should dedupe pending ticks per (workflow_id, schedule_id)

## Trigger

The dashboard's `wf-ui-triage` row on the operator overview showed six visible
tasks for a single periodic schedule (cron `7 */4 * * *`). Every one of them
was a synthetic Task under `SYSTEM_PLAN_ID` with `created_by='scheduler'`, no
`workflow_run_steps.started_at`, and no terminal `task.cancelled` /
`task.superseded` / `task.escalated_to_operator` event. Six logically
identical "do the triage" intents — one logical bot — were drawn as six
distinct surface rows because the dashboard derives `tasks` from per-task
state without folding scheduler dupes.

## Observation

`handle_scheduled_tick` dispatches a fresh synthetic Task per cron fire
(ADR-0057). When the role's start-to-first-step latency exceeds the cron
interval — which `wf-ui-triage` hit because its worker image cold-pull is
slow on the operator-scale workers — every new tick adds another pending
Task while the prior tick is still queued. Nothing in the dispatch path looks
backwards to ask "is there already a pending tick for this schedule?", and
nothing in the projection path collapses them. The intent (one bot doing
one job) and the materialization (N synthetic Tasks) drift apart on every
fire.

This is structurally the same shape as the `pr_synchronize` re-fire pattern,
except `pr_synchronize` is keyed by HEAD SHA so a re-fire under the same
SHA gets dedup'd by the dispatcher's idempotency probe. Scheduled ticks have
no equivalent key — the natural one is `(workflow_id, schedule_id)`, which
the scheduler knows but the dispatch path doesn't consult.

## Generalization

Recurrence primitives that produce an entity per fire need a dedupe key
keyed off the source identity (here: the schedule) plus a freshness
predicate (here: "no step has started"). Without it, latency spikes
silently amplify into surface backlogs proportional to spike duration ÷
fire interval. The amplification is invisible while it's happening — each
individual tick looks like a clean dispatch in logs.

A first-cut rule of thumb: any cron-driven producer that creates a
mutable downstream artifact (Task, Run, Job, …) should answer at dispatch
time, "what would I do if a prior fire's artifact is still pending?" The
defaults are coalesce-newer-wins (this case), let-them-stack (rare —
parallel runs by design), or coalesce-older-wins (almost never).

## Proposed rule

Scheduled-tick dispatch must coalesce prior pending ticks for the same
schedule before creating the new synthetic Task. "Pending" = the prior
tick's first `workflow_run_steps` row has `started_at IS NULL` and no
terminal lifecycle event has landed on the task. Coalesce = emit a
`task.cancelled` event with `reason='superseded_by_newer_tick'` keyed by
schedule_id and authored by `scheduler-coalesce`, so the audit log shows
which tick superseded which.

Explicit non-goal: an in-flight tick (its step's `started_at` is set) is
NOT cancelled. Parallel runs of the same schedule are allowed by design —
this rule collapses only the pending queue, not the running fleet.

Explicit non-goal: the deterministic-detector schedules
(`wf-stuck-task-sweep`, `wf-escalation-close-sweep`) do not synthesize
Tasks; their `handle_scheduled_tick` short-circuit fires before any
coalesce logic, so this rule does not apply to them.

## Proposed remediation

Helper `_coalesce_pending_ticks_for_schedule(session, dispatcher, *,
schedule_id, workflow_version_id) -> list[uuid.UUID]` lives in
`coordination/triggers.py` and is called from `handle_scheduled_tick`
right before `_dispatch_via_synthetic_task`, after the two deterministic
short-circuits. Query: `tasks WHERE plan_id=SYSTEM_PLAN_ID AND
created_by='scheduler' AND workflow_version_id=:wv AND NOT EXISTS
(started_at) AND NOT EXISTS (terminal task event)`. For each match, emit
`task.cancelled` via the existing `persist_and_publish` seam.

`workflow_version_id` is the on-task proxy for `(workflow_id, schedule_id)`
because the schedule_id isn't stored on the task row. In steady state a
schedule binds one workflow → one WV per tick, so the proxy is
load-bearing; a cross-WV pending (workflow re-seeded between ticks) is an
acknowledged corner case left alone.

## Notes

The six-row observation surfaced because the operator's first scan of
the Overview after the role-ui-triage prompt v1.5.0 deploy expected one
row, not six. The fix doesn't suppress the schedule from firing — that
would be wrong; it lets the scheduler keep firing on cadence and instead
collapses the pending queue at dispatch time. The cron stays unchanged;
the projection stays unchanged; only the dispatcher learns to fold the
queue.

A follow-up worth flagging: the same shape probably applies to
`wf-tune-judge-prompts` and any future periodic role-driven workflow.
The fix is keyed on the workflow_version_id (not on a per-workflow
allowlist), so it applies uniformly to all scheduler-driven synthetic
Tasks without per-workflow opt-in.
