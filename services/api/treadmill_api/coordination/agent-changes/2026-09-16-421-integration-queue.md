# 2026-09-16 — ADR-0119: integration_queue endpoint (api→host-integrator work-list)

The server half of ADR-0119 (integration executes host-side as the operator).

- `coordination/integration_queue.py` `approved_integration_candidates`: the SINGLE source of the
  approved-but-not-integrated candidate selection (Bert #419: the host reads this, never
  re-implements the query). Encodes the ADR-0118/#420 rules — latest approved head per task
  (`DISTINCT ON`), no `pr_merged`, no unresolved `integration_conflict`/`integration_blocked`
  escalation, feature-branch mode only. Joins `task_prs` for the pr_number matching the approved
  head and derives `joes-agents/<slug>` (`integration_slug` + `is_valid_ref_component`); an
  invalid slug is returned with `slug_valid=False` (host escalates), never dropped.
- `routers/integration_queue.py` + `app.py`: `GET /api/v1/integration_queue`, read-only (no side
  effects — escalation is the host's job).
- Verified: 32 consumer+queue DB foils green against real Postgres (ready candidate carries the
  branch/base/pr_number; latest-head only; merged/stuck/main-mode excluded; invalid slug flagged).
  No migration; returns [] when the router is dark.
- NEXT PR: the host-side integrator (systemd on rainbow, as the operator) consuming this + running
  `integrate_task` (incl. PR-ref fetch) + the enable runbook (the non-interactive operator
  credential the headless unit needs).
