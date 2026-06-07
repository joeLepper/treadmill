---
date: 2026-06-04
trigger: pattern
status: crystallized
crystallized_into: docs/knowledge-base/rules/host-process-restart-required-to-deploy.yaml
crystallized_date: 2026-06-07
related: ADR-0060, learning-2026-05-26 (deploy-watcher stale source)
---

# Learning: Host processes pin code at import time

## Trigger

PR #129 merged the egress-proxy allowlist fix (SQS/SNS/Secrets + bare
`github.com`), yet every `git clone` from workers kept failing with `CONNECT
tunnel failed, response 403` and the proxy kept logging
`{"hostname": "github.com", "decision": "deny"}`. The source file on disk
(`tools/local-adapter/treadmill_local/egress_proxy.py:38`) provably contained
the fix; `build_always_allowed()` run fresh in the same venv returned it. The
running autoscaler (started 16:47, minutes before the merge landed in the
tree) was writing per-worker allowlists from its in-memory
`_ALWAYS_ALLOWED_STATIC`, which predated the fix.

## Observation

We verified the fix three ways against the *source tree* (file content, git
show, a fresh interpreter import) and all three lied about the *system*: the
long-lived host process had imported the module before the merge, and an
editable venv makes this maximally deceptive — every fresh import sees new
code; the resident process never re-imports. The artifact it writes (the
per-worker allowlist JSON) was the only honest witness.

## Generalization

Third member of the stale-code family, after deploy-watcher-builds-from-
stale-source (2026-05-26) and stale containers (`dev-local always runs latest
main`). The family rule generalizes: code is deployed when the *process
serving it* restarts, not when the file changes — containers, watchers, and
now bare host processes alike.

## Proposed rule

After merging a change to code that a long-lived process serves (autoscaler,
deploy-watcher, API), the merge is not done until that process is restarted
and the fix is verified from an artifact the process produces — never from
the source tree.

## Proposed remediation

Deterministic check candidate: `treadmill-local up` (and the deploy-watcher)
could compare each managed process's start time against the newest commit
touching its package and warn `process older than its code`. Until then:
restart the owning process as part of landing any adapter-side fix, and
verify via process-produced evidence (e.g. `grep github.com` on the newest
per-worker allowlist JSON, not on `egress_proxy.py`).

## Notes

Sibling learnings: `2026-05-21-detached-subprocess-logs-unbounded-and-traceback-spam.md`
(same subsystem), deploy-watcher stale source (fixed via `_sync_local_to_origin`).
The fix-verification asymmetry is the keeper: *file fresh ≠ process fresh*,
and editable installs widen that gap rather than closing it.
