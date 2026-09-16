# 2026-09-16 — ADR-0118: router worker-assignment (roster + continuity + load-balance)

- `coordination/dispatch_consumer.py`: replaced the phase-1 placeholder `_resolve_worker` (one
  synthetic label) with the real assignment policy — a port of the legacy coordinator template
  §4 "Worker routing".
  - `select_worker` (pure core): CONTINUITY first (a rework returns to the worker that served
    the prior generation, when still in the roster), else LOAD-BALANCE (least in-flight-loaded
    roster worker, tie-broken by roster order). `None` on an empty roster.
  - `_resolve_worker` (async): roster = `team_configs.worker_labels`; prior = the task's newest
    author-execution worker; load = distinct non-terminal (no `pr_merged`) tasks per roster
    worker in the repo. Falls back to a deterministic synthetic label when a repo has no team
    config (the router is dark without a real team). `_dispatch` awaits it.
- Verified: 6 pure `select_worker` foils; 3 real-DB foils (initial load-balances to the emptier
  worker; rework reuses the prior worker; no-team-config → synthetic). Full coordination suite
  (69 tests) green against real Postgres. No migration.
- FOLLOW-ON: liveness (roster is the CONFIGURED set; picking only among running sessions is a
  further enhancement) and the file-area continuity bias from §4 (never persisted).
