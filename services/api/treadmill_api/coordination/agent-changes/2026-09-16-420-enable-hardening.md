# 2026-09-16 — ADR-0118: enable-hardening (worker-base topology + sweep fixes)

Mechanism-independent enable-step fixes (correct for either REST-merge or local-git
integration), shipped ahead of the enable slice. Both flagged by Bert on #418/#419.

- `tools/team-templates/worker/CLAUDE.md.tmpl` (router mode): the worker must PR into the plan's
  INTEGRATION BRANCH `joes-agents/<slug>`, NOT `integration_base` (which is only the cut-FROM
  ref, default main). A main-based PR would never close on integration → `github.pr_merged`
  never fires → dependents stall. Load-bearing seam corrected.
- `coordination/dispatch_consumer.py` `integration_sweep`:
  - LATEST-HEAD: `DISTINCT ON (task) ORDER BY created_at DESC` → integrate only the latest
    approved head per task (a re-approval must not re-merge the superseded head).
  - STUCK-EXCLUSION: skip an approval that already escalated (`integration_conflict` /
    `integration_blocked`) — re-driving would re-escalate every tick forever; a human clears it
    via a fresh verdict.
- Verified: 27 consumer DB foils green against real Postgres, incl. two new sweep foils
  (latest-head integrates only H2; a conflict-escalated approval is excluded, not re-driven).
  Still dark (sweep skipped without a runner). No migration.
- NEXT (gated on the `treadmill[bot]` App having `pull_requests:write` — a Joe/security check):
  the REST enable slice — server-side create `joes-agents/<slug>` (REST, contents:write) + merge
  the approved worker PR (REST `PUT /pulls/{n}/merge`, pull_requests:write) — then a pilot flip.
