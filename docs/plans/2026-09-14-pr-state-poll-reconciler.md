---
auto_merge: false
---

# Plan: PR-state poll reconciler for webhookless repos (v1)

- **Status:** drafting
- **Date:** 2026-09-14
- **Related ADRs:** ADR-0113 (PR-state polling), ADR-0110/0112 (feature-branch teams)

## Goal

Let a Treadmill team run on a collaborator-only repo (no App / no webhook) by polling
open PRs' merge + CI state via the GitHub read API and synthesizing the identical
`github.pr_merged` / `task.ci_result` events through the shared webhook-ingress seam,
so the coordinator pipeline is unchanged. v1 is the minimum to dogfood the run-shape
P0 plan on `netlify/agent-runner-orchestrator`.

## Success criteria

1. A merged PR on a poll repo → a `github.pr_merged` event byte-identical to a real
   webhook one (same `task_id` resolved from `(repo, pr_number)`, same
   `events.commit_sha` = merge sha), within one poll cadence. A completed CI suite →
   the same `task.ci_result` the `ci_observer` emits.
2. Re-polling an already-recorded merge/CI emits NOTHING (idempotent) — proven by a
   foil that polls the same state twice and asserts one event.
3. The coordinator, fed a synthesized `github.pr_merged`, unblocks a
   `depends_on: task.<id>.pr_merged` dependent exactly as with a webhook.
4. The poller reads via the repo's collaborator credential (`gh --user <account>`),
   never the App token, and never switches the active gh account.

## Constraints / scope

### In scope
- A server ingest endpoint that runs `persist_and_resolve_webhook_event` from an
  OBSERVED state `{repo, pr_number, action, merge_sha | ci_conclusion+suite}`.
- A host-side `treadmill pr poll <repo> --account <acct>` CLI: read open `task_prs`
  for the repo (via the API), `gh pr view` each, call the ingest endpoint on a new
  transition, idempotent, single-flighted (flock, per the reconcile pattern).
- Foils: a DB foil for the ingest endpoint (task-id + commit_sha faithfulness +
  idempotency); CLI foils for the poller (merged→ingest, CI→ingest, no-change→skip,
  gh-error→skip-fail-closed, idempotent re-poll).

### Out of scope
- A per-repo `event_source` config column + auto-detection (ADR-0113 follow-up) — v1
  takes repo + account as CLI args and a systemd timer per poll-repo.
- Back-fill of PRs closed while polling was off (follow-up).
- Any change to the coordinator, `ci_observer`, or `depends_on` resolution — they must
  work unchanged, which is the whole point.

### Budget
Operator-team work (I drive; direct edits + PRs). Abort to a post-mortem if the
synthesized event cannot be made byte-identical to a webhook one (then the manual
surface's gaps would leak downstream).

## Sequence of work

1. **Ingest endpoint** (server): `POST /api/v1/github/poll-ingest` — takes the observed
   state, builds the normalized body the webhook would carry, runs the shared
   `persist_and_resolve_webhook_event` seam. Covers `pr_merged` + `check_run`/ci. DB
   foil: synthesized `github.pr_merged` resolves `task_id` and sets `events.commit_sha`;
   a second identical ingest is a no-op (the seam's github dedup).
2. **Poller CLI** `treadmill pr poll <repo> --account <acct>`: GET open `task_prs` for
   the repo; for each, `gh pr view <n> --repo <repo> --json state,mergeCommit,merged,
   statusCheckRollup` under `GH_TOKEN=$(gh auth token --user <acct>)`; on a NEW
   transition (merged, or a completed suite) call the ingest endpoint; skip on no
   change or a gh error (fail-closed); flock single-flight. CLI foils (mocked gh + API).
3. **Timer + dogfood**: a `--user` timer for the poll repo; then onboard
   `netlify/agent-runner-orchestrator` (`team up`, feature-branch mode), and run
   Donna's P0 plan 1-then-4 (one investigation task first to prove PR-open + CI +
   integration end to end through the poller, then release the other four).

## Risks / unknowns

- `gh pr view --json statusCheckRollup` shape vs the `ci_observer`'s expected
  `task.ci_result` payload — map carefully or the CI signal mis-fires. Foil the mapping.
- Whether the seam's github-event dedup (the `(entity_type, action)` 409 / observer
  idempotency) actually makes a re-poll a no-op — the idempotency foil proves it, or we
  add a poller-side "already emitted" check keyed on the events table.
- Rate limits under a tight cadence — bound the poll set to open `task_prs`, tune cadence.

## Post-mortem

(pending)
