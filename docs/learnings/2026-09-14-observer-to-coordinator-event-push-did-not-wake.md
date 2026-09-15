---
date: 2026-09-14
trigger: surprise
status: captured
related: ADR-0090, ADR-0113, ADR-0115
---

# Learning: a correctly-synthesized task.ci_result did not WAKE the coordinator

## Trigger
During the ADR-0113 netlify dogfood, the poller synthesized 5 `github.check_run_completed`
→ the ci_observer derived 5 `task.ci_result`, all persisted + attributed to the task
(DB-verified). The netlify coordinator was alive, its WS was connected (accepted socket
in the API logs), and the `task_id → plan → repo → team_configs.coordinator_label`
resolution was intact — yet the coordinator showed NO activity for ~30 min after the
events landed. It was never woken; the task stayed "executing".

## Observation
The event was PERSISTED and PUBLISHED correctly, but the observer→coordinator DELIVERY
(the push that wakes the coordinator's session) did not fire. Direct messaging worked
(a tmux/`send` nudge woke the coordinator immediately), which localizes the gap to the
EVENT-PUSH path (publish → SNS/SQS → fabric_event_sink / dashboard-ws → coordinator
inbox), not to the coordinator session, the WS connection, or the resolution logic.
`plan.submitted` DID wake it (standup ran) — that uses the payload-`coordinator_label`
fast path; the task-scoped `task.ci_result` uses the `task_id`→…→`coordinator_label`
resolution, and that push did not reach the session.

## Generalization
Persisted-and-published is NOT the same as delivered-and-woken. Any coordinator step
gated on an EXTERNAL event waking the session — `task.ci_result` (CI gate) AND
`github.pr_merged` (the §3.3 `depends_on` resolver) — can stall silently even when the
event is correct in the store. Task-scoped events (resolved to a coordinator via
`task_id`) appear more affected than payload-routed events (`plan.submitted`).

## Proposed rule
Do not rely on the observer→coordinator event push for a load-bearing coordinator step
without confirming the wake actually fires end-to-end. Prefer SELF-DRIVEN resolution
where the coordinator has the ground truth (ADR-0115: feature-branch mode resolves
`depends_on` inline after its own integration, and gates on peer-review not ci_result,
so it never depends on this push). For main-merge repos that MUST gate on events, the
push path itself needs a fix (must-fix), plus a reconcile/catch-up backstop.

## Proposed remediation
(1) Diagnose + fix the event-push wake for task-scoped events (fabric_event_sink /
dashboard-ws delivery) — a must-fix for main-merge mode. (2) A periodic coordinator
reconcile (re-read active tasks' event state) as a backstop against a missed wake, like
the ADR-0109 reconcile watcher. (3) ADR-0115 removes feature-branch mode's dependency on
this path entirely.

## Notes
Direct-message nudge (tmux send-keys / `send`) is the operational mitigation and
confirmed working; it woke the coordinator and it self-drove peer-review → integrate.
Caught live on the netlify dogfood 2026-09-14. Related: [[feedback_send_held_means_channel_drop_restart]].
