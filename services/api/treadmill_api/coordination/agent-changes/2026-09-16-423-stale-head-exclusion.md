# 2026-09-16 — ADR-0119: by-(task,head) stuck-exclusion (Bert #422 Q3, enable-gating)

- `coordination/integration_queue.py`: the queue stuck-exclusion is now by (task, HEAD), not by
  task, and includes `integration_stale_head`. It excludes a candidate whose (task, head_sha)
  already escalated (`integration_conflict`/`blocked`/`stale_head` with `payload.head_sha` ==
  the candidate head), but lets a FRESH approval at a NEW head through. Fixes the un-ackable
  incident + unbounded events growth: `stale_head` (a common post-approval push) was NOT excluded,
  so a head-moved candidate re-selected + re-escalated every poll, and each new escalation row
  advanced `opened_at` past the operator's ack. Also closes the #420 conflict/blocked re-approval
  hole uniformly (a resolved+re-approved head is no longer permanently ignored).
- `router_integrator.py`: the escalation payload now carries `head_sha` (load-bearing for the
  by-(task,head) exclusion).
- `tools/router-integrator/AGENT.md`: credential nits (Bert #422 Q4) — `credential.helper store`
  is plaintext `~/.git-credentials` (chmod 600 + warn); prefer a fine-grained PAT
  (contents:write + pull_requests:write per-repo) over classic `repo` scope; a `--user` unit
  needs `loginctl enable-linger` on a headless host.
- Verified: 34 consumer/queue DB foils green vs real Postgres, incl. 2 new (a stuck head is
  excluded but a fresh new-head approval flows; the only-approved-head-still-stuck stays empty).
- FOLLOW-UP (Bert #422, not enable-gating): infra-retry bound → escalate `integration_infra_stuck`
  (fetch/merge/push failures re-drive every poll unbounded today, visible only as a log warning).
