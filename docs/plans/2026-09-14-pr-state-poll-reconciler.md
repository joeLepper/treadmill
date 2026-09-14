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

1. **Ingest endpoint — MERGE leg** (server): `POST /api/v1/github/poll-ingest` for
   `pr_merged` — builds the `pull_request` closed+merged body, runs the shared
   `persist_and_resolve_webhook_event` seam, gates on the deterministic event_id's
   prior existence to avoid the seam's unconditional re-publish. DONE (dd40888) — DB
   foils prove byte-identity (task_id + commit_sha) and no re-publish on re-poll.
2. **Ingest endpoint — CI leg**: `github.check_run_completed` (NOT `task.ci_result` —
   the seam derives that via `ci_observer`). `POST /api/v1/github/poll-ingest/check-run`
   takes an observed completed suite `{repo, pr_number?, head_sha, check_suite_id,
   conclusion, app_slug}`, synthesizes the completed-suite snapshot
   (`check_suite.status=completed`), and routes it through the shared seam. Idempotency
   gate keyed on `(check_suite_id, head_sha, conclusion)` so a CI re-run with a changed
   conclusion re-emits. DONE — DB foils prove the observer derives the same
   `task.ci_result` a webhook would (suite-id + app_slug reconstruction) AND the
   conclusion-change re-emit vs same-conclusion no-op. Both foils red-then-green
   verified by mutation (suite-not-completed, conclusion-dropped-from-key). (Ernie
   CI-leg co-sign pending.)
3. **Poller CLI** `treadmill pr poll <repo> --account <acct>`: GET open `task_prs` for
   the repo (new `GET /api/v1/task_prs?repo=&open=true`); per PR, `gh pr view <n>
   --json mergeCommitOid,headRefOid,merged,state` and `gh api
   .../commits/<sha>/check-suites` under `GH_TOKEN=$(gh auth token --user <acct>)` —
   NEVER `gh auth switch`; call the ingest endpoint on a new transition; skip on no
   change or ANY gh non-zero (fail-closed, per-leg); per-repo flock single-flight.
   DONE — 8 CLI foils (injected gh + fake client): merge+CI ingest, no-change→nothing,
   gh-error→fail-closed (both legs, independent), token-error→abort, idempotent
   re-poll not counted, only completed suites ingest, per-repo single-flight. Ernie
   CLI co-sign pending.
4. **Timer + dogfood**: a `--user` timer for the poll repo; then onboard
   `netlify/agent-runner-orchestrator` (`team up`, feature-branch mode), and run
   Donna's P0 plan 1-then-4 — one investigation task first to prove PR-open + CI +
   integration end to end through the poller (and to confirm whether the repo runs CI
   on these PRs at all), then release the other four.
   - Timer units DONE: `tools/cc-channels/systemd/treadmill-pr-poll@.{service,timer}`
     (per-repo instance = slug; REPO + ACCOUNT from an EnvironmentFile; NOT
     auto-enabled — enabling begins synthesizing events, the operator's call).
   - Dogfood BLOCKED: the treadmill API is down (the `treadmill-api` container exited
     ~2 weeks ago; no `treadmill-local` host processes run). Bringing it up needs
     `treadmill-local up` from MAIN (branch-sensitive; a cold full-stack bring-up on
     the shared host) — an operator decision, surfaced to Joe. The dogfood also holds
     for Ernie's slice-3 co-sign.

## Risks / unknowns

- `gh pr view --json statusCheckRollup` shape vs the `ci_observer`'s expected
  `task.ci_result` payload — map carefully or the CI signal mis-fires. Foil the mapping.
- Whether the seam's github-event dedup (the `(entity_type, action)` 409 / observer
  idempotency) actually makes a re-poll a no-op — the idempotency foil proves it, or we
  add a poller-side "already emitted" check keyed on the events table.
- Rate limits under a tight cadence — bound the poll set to open `task_prs`, tune cadence.

## Post-mortem

(pending)
