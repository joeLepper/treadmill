# tools/cc-channel-treadmill

## Purpose

This directory holds the `treadmill-events` Claude Code channel server (Bun,
stdio MCP): the push path that turns Treadmill dispatched-work lifecycle
events into `<channel source="treadmill-events">` wakes inside the
originating Claude Code session, replacing per-session poll monitors
(ADR-0068). It also watches the session's relay inbox
(`~/.cc-channels/<label>/relay/`) and injects `cc-relay.py` messages as
channel notifications. Since ADR-0089 it is additionally the wake-economics
control point: a per-session wake filter decides which event classes wake
the session at all, with suppressed events digested rather than dropped.

## Key surfaces

- `treadmill-events.ts` — the channel server. WS connect/backoff loop
  against `/api/v1/dashboard/ws/events`, client-side ownership enforcement
  (`isMine` + throttled reconcile), reconcile-on-connect catch-up frames,
  event dedup, the relay-inbox watcher (base dir + `coord`/`worker`
  subfolders), and the ADR-0089 wiring: wake gate on the event path,
  digest prepend on delivered wakes, the bounded-blindness timer, and the
  startup wake ⊇ relay WARN. Identity and config come from env
  (`TREADMILL_SESSION_LABEL`, `TREADMILL_ROLE`, `TREADMILL_WAKE_ACTIONS`,
  `TREADMILL_MAX_SUPPRESSION_AGE`, `TREADMILL_RELAY_LEVEL`, …) — see the
  file header for the full table.
- `wake-filter.ts` — pure ADR-0089 logic, no I/O: `entity.action` glob
  parsing (`parseWakeActions`, role defaults — orchestrator gets
  `ORCHESTRATOR_DEFAULT_WAKE_ACTIONS` incl. the enumerated terminal plan
  outcomes `plan.completed`/`plan.abandoned`, every other role
  unfiltered), the `WakeGate` suppression state machine (per-action
  digest counters; two-phase peek → notify → `markDelivered()` commit so
  a failed notification never loses the digest; max-suppression-age
  bounded blindness; injectable clock), and `wakeSetViolations` for the
  wake ⊇ relay superset invariant.
- `wake-filter.test.ts` — the `bun test` suite pinning role defaults
  (incl. the two ENUMERATED escalation-class actions
  `task.evaluator_timeout` / `task.rework_exhausted`), glob semantics,
  digest accumulate/reset, the suppressed-only-stream digest wake, and
  the superset WARN's violation list.
- `README.md` — operator-facing setup (Bun install, user-scope MCP
  registration, launch via `tools/cc-channels/launch-session.sh`), env
  table, smoke test.

## Recent changes

- **ADR-0090 coordinator + evaluator wake allowlists (task fe98030f — the cb3d0c29 finale)**: `wake-filter.ts` gains `COORDINATOR_DEFAULT_WAKE_ACTIONS` + `EVALUATOR_DEFAULT_WAKE_ACTIONS`, wired through `parseWakeActions` role branches (the `launch-session.sh` `TREADMILL_ROLE` export already arms them for team labels via their .env files). Both sets EXCLUDE the two noise classes by design — `github.check_run_completed` (replaced by the API observer's one-per-suite `task.ci_result`, #336/#337) and `github.pr_synchronize` (push noise) — and both ENUMERATE the escalation-class actions per the #310 invariant. Coordinator keeps every §3-handler decision/lifecycle class incl. `plan.submitted` (its #310 pickup signal — load-bearing here, unlike the orchestrator set where it's an echo); evaluator gets the decision/escalation core + `task.ci_result` + the `review.*` handoff family, with bookkeeping classes deliberately out (briefs arrive via relay, which always wakes). The RELAY vocabulary dropped the same two classes: `task.ci_result` replaces `check_run_completed` at `normal` (the ci-fix loop now enters on the rollup), `pr_synchronize` left `verbose`. Both new role sets satisfy wake ⊇ relay at quiet AND normal; verbose's `step.*` remainder is the documented accepted WARN tier (orchestrator precedent). Worker stays unfiltered (measure first). Tests: 38 (13 new — per-role wake sets incl. the forbidden-failure escalation guard for both roles, both exclusions, evaluator bookkeeping-out, superset invariants per level, relay-vocabulary pins).

- ADR-0089 wake-class filtering (task 9b7c1286, 2026-06-11) —
  `TREADMILL_WAKE_ACTIONS` globs / role defaults / suppression digest /
  max-suppression-age digest wake / wake ⊇ relay startup WARN; new
  `wake-filter.ts` + `bun test` suite. Companion server-side fix in
  `services/api/.../routers/dashboard/ws.py`: `plan.submitted` now matches
  the payload's `coordinator_label` (no DB race) and negative owner
  lookups expire instead of blinding the socket forever. Review-cycle
  hardenings (same PR): quiet relay set enumerates
  `task.evaluator_timeout`/`task.rework_exhausted` so custom wake sets
  can't silently mute them; digest delivery is peek → commit (failed
  notifications retain counts); the ws payload fast path is gated to
  `plan.submitted`; `plan.completed`/`plan.abandoned` added to the
  orchestrator defaults (orchestrator ruling + ADR-0089 amendment).
- ADR-0086 — `plan.submitted` client pass-through for coordinators and
  `?coordinator_label=` on the WS URL so a coordinator picks up new
  plans in-session.
- ADR-0084 — coordinator subscription widening (`?plan_ids=`,
  `TREADMILL_COORDINATOR_PLANS`) and role-prefixed relay subfolders.

## Pitfalls

- The wake gate sits BELOW the ADR-0071 relay level: relay levels select
  from events that already woke the session. A wake set that drops a
  relay-significant action can silently mute the operator — that's the
  startup WARN. When adding a new escalation-class action whose name
  escapes the `task.escalat*` glob, you MUST add it to
  `ORCHESTRATOR_DEFAULT_WAKE_ACTIONS` (a filtered-away escalation is the
  design's one forbidden failure mode).
- Digest counters are in-memory only; a server restart loses suppressed
  counts. Acceptable by design — the events table is the record.
- Relay messages and reconcile frames always wake and are never counted
  as suppressed; the digest line rides delivered EVENT/reconcile wakes
  only (relay bodies are sender-attributed content and must not get
  server text prepended).
- `TREADMILL_MAX_SUPPRESSION_AGE` is in MINUTES (default 60). The digest
  wake lands within age + one check period (60 s).
- Do NOT point `TREADMILL_API_URL` at the `:8080` auth proxy — it serves
  REST but does not upgrade WebSockets ("Expected 101"); use the direct
  API port (`:8088`).
- The server is registered user-scope and spawns in EVERY session; it
  stays inert (no WS, no notifications) without `TREADMILL_SESSION_LABEL`.
  Don't "fix" the missing-label path into an exit — MCP health must stay
  green in unlabelled sessions.
- Channels are a research preview (pinned against Claude Code 2.1.161);
  re-verify the `--dangerously-load-development-channels` contract after
  CC upgrades.

## Navigation

- **Adjacent:** `tools/cc-channels/` (launcher, relay, systemd/tmux
  supervision — sets this server's env); `services/api/treadmill_api/
  routers/dashboard/ws.py` (the WS feed this server consumes, with the
  server-side created_by / plan_ids / coordinator_label filters);
  `services/api/treadmill_api/eventbus.py` (the in-process broadcast the
  WS feed rides).
- **Decisions:** ADR-0068 (channel + identity model); ADR-0071 (relay
  verbosity layers); ADR-0084 / ADR-0086 (coordinator subscription and
  plan pickup); ADR-0089 (wake filtering + digest + superset invariant);
  ADR-0062 (escalation taxonomy the relay sets pin to).
- **Follow:** Start with ADR-0068 for the transport/identity contract,
  then ADR-0089 for the wake-economics layer; `wake-filter.ts` is
  self-contained reading for the filter semantics.
