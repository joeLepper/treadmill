# 2026-09-16 — ADR-0119: host-side router integrator (approve→integration executor)

The execution half of ADR-0119 — a HOST process (rainbow, as the operator), never the container.

- `treadmill_api/router_integrator.py` (new; a HOST entrypoint, NOT started by the api lifespan):
  polls `GET /api/v1/integration_queue` and integrates each approved task's PR into
  `joes-agents/<slug>` via `integrate_task`, as the operator. `process_candidate` maps outcomes:
  merged/already-integrated → done; conflict → escalate `integration_conflict`; head-moved →
  escalate `integration_stale_head`; infra → left for the next poll. The two unactionable shapes
  (`slug_valid=False`, `pr_number=None`) ESCALATE, never skip (Bert #421). Per-candidate
  containment in `poll_once`.
- `coordination/integration_merger.py`: `MergeOp.pr_number` + `integrate_task` PR-ref fetch —
  fetches `refs/pull/<n>/head` and VERIFIES the tip == the approved `task_head`, returning
  `head-moved` if it moved (the TOCTOU guard: never merge unapproved content as the operator).
- `events/task.py`: `integration_stale_head` escalation reason.
- `tools/router-integrator/`: the systemd unit template (not auto-enabled) + `AGENT.md` (the
  credential provisioning + enable runbook).
- Verified: 20 pure foils (PR-ref verify/head-moved/fetch-fail; process_candidate outcome map;
  poll_once poison-containment) + 32 DB consumer/queue foils green against real Postgres.
- ENABLE (operator): provision a non-interactive operator gh credential on rainbow, then enable
  the unit + flip a pilot plan `substrate=router`. See tools/router-integrator/AGENT.md.
