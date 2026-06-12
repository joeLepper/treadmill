# `treadmill_api/coordination` — surviving background loops

Post-ADR-0087 Phase 5 this package holds the small set of background
loops that keep API state honest. The pre-ADR-0087 contents — the
step-event consumer pipeline (Phase 4) and the trigger evaluator +
deterministic health sweeps (Phase 5) — are gone with the
workflow_runs/workflow_versions tables they read.

## Key surfaces

- **`replay.py`** — `ReplayLoop`. Re-publishes events whose SNS publish
  failed, from the durable `DispatchPublishFailed` Event-row markers
  (A.8/A.10). As of Phase 5 the marker is written by
  `Dispatcher.persist_and_publish` itself (previously only the deleted
  `dispatch_task` path wrote it), so every HTTP emitter gets replay
  healing.
- **`webhook_inbox.py`** — `WebhookInboxPoller`. Drains the
  GitHub-webhook SQS inbox in dev_local / fully_remote modes (ADR-0017)
  through the shared `persist_and_resolve_webhook_event` helper.
- **`notification_fanout.py`** — `NotificationFanout`. Pushes operator
  notifications (Slack / raw-webhook targets) off the in-process event
  bus (ADR-0062).
- **`escalation_close_sweep.py`** — survives for
  `emit_operator_close`, which the escalations router calls to emit
  `task.escalation_closed` events with `expected_followup` annotations.
  Its periodic sweep loop has no caller since the consumer's removal;
  the coordinator's own monitoring replaces the health-sweep family
  per ADR-0087 §Health bots (follow-on track).

## Deleted in ADR-0087 Phases 4–5

Phase 4 (PR-F #297): `consumer.py`, `event_projector.py`,
`plan_router.py`, `redispatch.py`.
Phase 5 (PR-G): `triggers.py` (trigger evaluator + scheduled-tick
router + cap machinery), `coordinator_overlay.py`, `cross_step.py`,
`dispatch_dedup.py`, and the sweep family (`stuck_task_sweep`,
`terminal_gate_sweep`, `step_starvation_sweep`, `fleet_wedge_sweep`,
`auto_merge_loop`, `conflict_sweep`, `plan_doc_trigger`,
`unreferenced_close_report`).

Note: the scheduler subprocess (`treadmill_api/scheduler/`) still
publishes `ScheduledTick` events, but nothing consumes them — the
consumer that routed ticks died in Phase 4. Schedules are inert
pending the ADR-0087 §Health bots redesign.

## Recent changes

> **New entries are PER-PR FRAGMENT FILES, not prepends** (task
> 986c5cf6): add `agent-changes/YYYY-MM-DD-<task-or-pr-slug>.md` beside
> this AGENT.md — one entry per file, newest by filename; format in
> `docs/agent-md-schema.md`. Prepending here is the conflict factory
> that stacked three same-day rework cascades on 2026-06-12 (every
> in-flight PR inserts at this same anchor). Entries below predate the
> convention and are frozen; gardening folds them into the sections
> above.

- **ADR-0087 Phase 5 (PR-G)** — deleted the trigger evaluator + sweep
  family (above); moved the `DispatchPublishFailed` marker write into
  `persist_and_publish` so the ReplayLoop heals every emitter, not just
  the deleted dispatch path.
