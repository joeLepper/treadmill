# `treadmill_api/coordination` — scheduler tick routing & deterministic sweeps

The coordination layer intercepts scheduled ticks and event triggers, routing them to the appropriate workflow + step or to a deterministic detector bot. The sweeps are pure-query monitoring primitives (ADR-0035, ADR-0047, ADR-0075).

## Deterministic sweeps

Four scheduled sweeps run on the coordination consumer without materialized workflow runs:

- **``stuck_task_sweep.py``** — every 10 minutes, detects tasks whose most recent event is older than 30 minutes and whose latest step is ``step.completed`` with no later ``step.ready`` (the downstream step never dispatched). Emits ``task.escalated_to_operator`` with ``reason='stuck_task_sweep'``.

- **``escalation_close_sweep.py``** — every 2 minutes, detects open operator escalations (latest ``task.escalated_to_operator`` with no later ``task.escalation_closed`` for the same task) and closes them when the underlying task hit a close trigger (``re_progressed`` / ``pr_merged`` / ``cancelled`` / ``superseded``). Emits ``task.escalation_closed`` for each.

- **``terminal_gate_sweep.py``** — every 10 minutes, detects tasks with architect accept-as-is verdicts (``review.override`` / ``validate.override`` per ADR-0038 / ADR-0042) whose PRs were never merged. Emits ``task.escalated_to_operator`` with ``reason='terminal_gate_sweep'``.

- **``step_starvation_sweep.py``** — every 1 minute, detects steps queued for worker dispatch (``step.ready``) that never reached execution (``step.started``). When a step's most recent event is ``step.ready`` and is older than 5 minutes with no later ``step.started`` for the same (task, step_index), the sweep escalates it. Emits ``task.escalated_to_operator`` with ``reason='step_starvation'`` naming the step and role.

All four sweeps:
- Are idempotent at the SQL layer (``NOT EXISTS escalated_to_operator`` clause).
- Are routed by ``handle_scheduled_tick`` in ``triggers.py`` via workflow_id slug interception, short-circuiting the normal ``WorkflowVersion`` lookup.
- Seed their schedules in ``seed/schedules.py`` (auto-seeded on fresh deploy).

## Recent changes

- **ADR-0083** — `ArchitectEmitFailure` event + relay-drop trigger. When the architect role fails to emit a structured verdict via `--json-schema`, the worker POSTs to `POST /api/v1/tasks/{id}/architect_emit_failure`. The consumer calls `maybe_drop_relay_on_architect_emit_failure` (triggers.py) which writes a markdown file to `~/.cc-channels/<created_by>/relay/` so the dispatching orchestrator session learns about the failure. The relay dir is configurable via `TREADMILL_CC_CHANNELS_DIR`; a volume mount is required when the API runs in Docker. Production-split deployments need a remote-drop mechanism — out of v1 scope. Rollback: remove the `architect_emit_failure` endpoint and handler; the event type stays in the registry for audit-log compatibility.

- **ADR-0079** — Dispatcher short-circuits `step.ready` on terminal task status. When a task reaches a terminal status (`pr_merged`, `cancelled`, `superseded`, `escalation_closed`) and an action-class workflow (`wf-author`, `wf-feedback`, `wf-architecture-resolve`) has a pending step, `dispatch_next_step()` in `cross_step.py` emits `StepSkipped` event instead of `StepReady` and skips SQS work-queue assignment. New event payload `StepSkipped` (events/step.py) carries `reason` and `terminal_status` fields. Consumer projection in `_dispatch_step()` updates step status to `'skipped'`.

- **PR #???** — Added `expected_followup` field to `TaskEscalationClosed` event and `CloseRequest`/`CloseResponse` models. Auto-closes from the escalation-close sweep write `transient:auto_progress`; operator closes can optionally specify `learning:<slug>`, `pr:<number>`, `adr:<NNNN>`, or `transient:<cause>` to document intended followup. Null/empty values are counted as unreferenced.

- **PR #???** — Added `unreferenced_close_report.py` sweep that fires weekly (Mondays 09:00 UTC). Sweeps past 7 days of `escalation_closed` events with null/empty `expected_followup`, groups by repo, and emits one `system.unreferenced_closes_report` event per repo for NotificationFanout (ADR-0062) to consume and alert operators.
