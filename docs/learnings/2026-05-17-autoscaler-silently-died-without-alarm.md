---
date: 2026-05-17
trigger: surprise
status: captured
related: TaskList-#136
---

# Learning: the autoscaler can silently die and leave the system half-alive

## Trigger
On 2026-05-17, after merging the auto-merge papercut plan (PR #134) and its activation (PR #135), the plan's 10 tasks registered correctly and dispatched into SQS — but no workers picked them up. Investigation showed the autoscaler process (PID 1353427) had been "alive" by `ps` for 1 day 8h, but had not written any log entries since 2026-05-16 09:55 (36 hours of silence). Its last log lines were `tick: depth=1 current=0 desired=1 started=1` followed by `received signal; stopping` — a graceful shutdown that never came back up cleanly. The Python process remained as a zombie of sorts: alive but not ticking.

Meanwhile the API, coordination consumer, and webhook handlers stayed healthy and kept processing events. From the outside, the system *looked* fine — events flowed, plan ingestion worked, the DB updated. The only symptom was that author/validate/review/feedback workflow runs queued indefinitely without ever getting picked up by a worker.

## Observation
A process being "alive" per `ps` is necessary but not sufficient for liveness. The autoscaler's silent death was indistinguishable from "queue is empty" until SQS depth grew and no workers spawned. The proper restart had to go through `treadmill-local up --no-build` because direct invocation of `python -m treadmill_local.autoscaler` requires three env vars (`TREADMILL_INFRA_DIR`, `TREADMILL_AUTOSCALER_FAMILY`, `TREADMILL_AUTOSCALER_QUEUE_URL`) that only the runtime wrapper knows how to populate.

## Generalization
We have no liveness signal for components that can fail silently — the autoscaler is one, and the worker fleet itself, the coordination consumer's redispatch loop, and any future scheduled-job runner are sibling-shaped. Long-running supervisors should emit a heartbeat that downstream observers can alarm on, not just write tick-by-tick log lines that nobody reads.

## Proposed rule
A long-running supervisor process owns a heartbeat: it must write a timestamped liveness marker (file, DB row, OTel gauge) at least every `tick_seconds × 2`, and any health endpoint or operator UI must surface "last heartbeat older than 5× tick" as a red signal.

## Proposed remediation
- Add `last_tick_at` to a `system_pulse` table or Redis key, written by every autoscaler tick.
- `treadmill-local status` checks this and reports stale-autoscaler with a loud warning.
- The TaskList #136 in-flight UI must include autoscaler/consumer pulse as a top-level widget.
- Promote the autoscaler's tick log line from current `INFO` to also write a per-tick `entity_type='autoscaler_pulse'` event; the events table becomes the durable signal source.

## Notes
- Direct restart attempts via `python -m treadmill_local.autoscaler` fail with `KeyError: 'TREADMILL_INFRA_DIR'` — always use `treadmill-local up --no-build` instead.
- The hung-but-process-alive failure mode is harder to detect than crash-loop because there's no PID churn. Sibling shape: see learning 2026-05-17-auto-merge-trigger-loses-race-with-validate-override (silent bailouts that produce no observable signal).
