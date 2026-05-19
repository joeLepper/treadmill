# Handoff: 2026-05-18 hands-free reliability push

**Audience:** Joe, returning to the project after stepping away.
**Status as of writing:** in-progress — handoff drafted preemptively per Joe's "if by some miracle you finish this all" directive.

This document captures the state of Treadmill as of 2026-05-18 after a multi-session reliability push, the substantive changes shipped, and what's safe to expect when picking back up.

## TL;DR

- Treadmill's hands-free auto-merge path is materially more reliable than at start of day.
- 5 fresh auto-merges fired today (PRs #153, #156, #157, #159, #160). 1 of the 5 was fully autonomous (#157); the others required operator nudges that have since been replaced with durable fixes.
- 8 operator-side reliability PRs shipped (#150, #152, #154, #158, #161, #162, #163, plus heartbeat refactor in #148).
- 2 ADRs accepted: ADR-0046 (operator task retry CLI, design only) and ADR-0047 (replace LLM non-determinism with deterministic state in auto-merge path — the principle that ties the session's changes together).
- 1 implementation plan in flight: ADR-0046 task-retry CLI (PR #166, status: drafting).

## What changed in code

| PR | What | Why it matters |
|---|---|---|
| #148 | Heartbeat refactor (pulse file → log mtime) | Don't reinvent logging. Net code reduction; same operator signal |
| #150 | role-ci-analyzer + role-code-author anti-"already in place" forbid | Stopped the wf-ci-fix infinite loop pattern observed on #149 |
| #152 | wf-author step.failed → wf-feedback retry | Silent worker death no longer zombies the task |
| #154 | Auto-merge predicate fires on wf-architecture-resolve (then #163 removes the workflow-set gate entirely) | Architect override now reaches the cooling-off deadline |
| #158 | Demoted validation-script-executed LLM-judge to warning | Removed a redundant LLM check whose deterministic sibling already passes |
| #161 | `--dev` fast-path honored in dev_local | `treadmill submit` intent-only no longer creates inert plans |
| #162 | role-reviewer anti-spurious-changes_requested forbid | Trivial PRs no longer cycle review-feedback before approving |
| #163 | Auto-merge predicate fires on any step.completed (drop workflow gate) | Future override channels Just Work without trigger-set bookkeeping |

## What changed in docs

- **ADR-0042** — validate.override channel (accepted earlier in session)
- **ADR-0046** — operator task retry CLI (proposed → accepted)
- **ADR-0047** — replace LLM non-determinism with deterministic state in auto-merge path (accepted)
- **Plan: 2026-05-16-validate-override-channel.md** — corresponding plan for ADR-0042
- **Plan: 2026-05-18-adr-0046-task-retry-cli.md** — 5-task implementation plan for ADR-0046 (status: drafting, PR #166)
- **Learnings (new this session):**
  - `2026-05-17-auto-merge-trigger-loses-race-with-validate-override.md` — addressed by #142
  - `2026-05-17-autoscaler-silently-died-without-alarm.md` — addressed by #147 then #148
  - `2026-05-18-heartbeat-reinvented-logging.md` — the "don't add a second substrate" principle
  - `2026-05-18-wf-author-pr-body-leaks-session-narration.md` — addressed by #151
  - `2026-05-18-wf-ci-fix-analyzer-skips-actual-logs.md` — addressed by #150
  - `2026-05-18-auto-merge-misses-architect-override-completion.md` — addressed by #154 then #163

## What's running

- API container: `treadmill-api` on `treadmill-api:dev` image (post-#163 if rebuild succeeded; check `docker ps`)
- Postgres on host port 15432
- Redis on host port 16379
- Autoscaler subprocess (PID written to `tools/local-adapter/.treadmill-local/autoscaler.pid`; heartbeat via `autoscaler.log` mtime)
- Scheduler subprocess (PID written to `scheduler.pid`)
- DB role prompts (per ADR-0028) updated this session: `role-code-author` (v4 — anti-already-in-place + PR-body forbid), `role-ci-analyzer` (v3 — mandatory log fetch + forbid-list), `role-reviewer` (v3 — anti-spurious-changes_requested forbid)

## What's stuck

Tasks still in `wf-feedback: failed` or `blocked` that the post-fix system can't naturally retry without the ADR-0046 retry CLI:

- `automerge-pipeline-papercuts` plan: 4 wf-feedback: failed tasks (`task-cancelled filter`, `pr_closed event verb`, `reconcile task_status`, `architect override flush` — last one is shipped in #142 but projection lag), 2 blocked tasks, 1 wf-validate: failed (depends_on smoke).
- `periodic-ops-bots-first-wave` plan: `wf-rule-corpus-health-workflow` (wf-feedback: failed), `smoke-four-seeded-schedules` (wf-validate: failed), `seed-rule-corpus-health-schedule` (blocked).
- 24 tasks at `review_passed` projection-lag — PRs merged on GitHub but the task_status VIEW projects review_passed because the wf-review run was the most-recent and overrode the pr_merged check. This is the projection-bug captured but not fixed; deferred as cosmetic.

## How to pick up

1. **Check what auto-merges fired overnight:**
   ```
   docker exec treadmill-redis redis-cli KEYS 'treadmill:auto-merge-fired:*'
   ```
2. **Check open PRs:**
   ```
   gh pr list --state open --limit 20
   ```
3. **Survey stuck tasks:**
   ```sql
   SELECT substring(p.doc_path FROM 'plans/(.+)\.md') AS plan, ts.derived_status, count(*)
   FROM tasks t JOIN task_status ts ON ts.id = t.id JOIN plans p ON p.id = t.plan_id
   WHERE p.doc_path IS NOT NULL AND ts.derived_status NOT IN ('pr_merged','done','cancelled')
   GROUP BY plan, ts.derived_status
   ORDER BY plan, ts.derived_status;
   ```

## What's the next pass

The current target Joe set is **10 more auto-merges** of real plan work tonight (no synthetic smokes).

**Status at handoff time (live):**
- PR #166 (ADR-0046 implementation plan, 5 tasks) — **MERGED**; plan auto-activated to status=active; first task `d09af3d4` (TaskRetry event class) in `wf-author: executing`.
- Synthetic nudge fired on `46e8ba5d` (alembic heads CI step, wf-author: failed) — wf-feedback dispatched, 2 pending steps.
- Background watcher tracking the next 6 auto-merge-fired keys (`/tmp/claude-1000/.../tasks/bg1hl6qgl.output`).
- Other 5 wf-feedback: failed papercut tasks (e7ffc11e task.cancelled, 02789bf6 reconcile, 9b81e083 pr_closed, 4b879ca7 architect override flush already-shipped projection-lag, 8dce5394 wf-rule-corpus-health) **cannot be naturally retried** without ADR-0046's retry CLI — they have completed wf-feedback runs that exhausted the natural deadlock-arbitration path. Mass-retry candidates after the CLI lands.

**Sequenced ADR-0046 implementation chain (5 PRs expected):**
1. TaskRetry event payload + registry — `d09af3d4` (in flight)
2. infer_retry_workflow helper — `efec9c76` (blocked on 1)
3. POST /tasks/{id}/retry endpoint — `f0fb0a3e` (blocked on 1+2)
4. `treadmill task retry` CLI command — `2362fcfa` (blocked on 3)
5. Smoke handoff doc — `110ed1ca` (blocked on 4)

Each step in the chain should auto-merge given the round-down fixes (#150 / #152 / #154 / #158 / #161 / #162 / #163) are all live in DB / image. If any task gets stuck on a NEW failure mode I haven't seen yet, that's the next durable ADR-0047 instance.

**After the chain merges:**
- The retry CLI is live. Mass-retry the 5+ wf-feedback: failed papercut tasks. Each retry under the post-round-down system should flow cleanly to auto-merge.
- That should put auto-merge count comfortably past 10.

**If you want overnight autonomous progress:** the system can run with the current state. The scheduled bots (periodic-* schedules) tick every 10/15 min, but their bound workflows (wf-stuck-task-sweep, wf-o11y-regression-scan, wf-rule-corpus-health) are NOT seeded — they tick but no-op. Seeding those requires the `wf-rule-corpus-health-workflow` papercut task (`8dce5394`) to merge, which is wf-feedback: failed today. Post-retry-CLI mass-retry would unstick it; once merged + redeployed, scheduled bots would actually fire actual workflows.

## Open threads

- **CI re-runs from operator pushes:** verified to fire fresh wf-validate + wf-review (per the 22:50:07 timestamps I observed on PR #156). Not a separate gap.
- **`task_mergeability.changed` projection-event** as the cleaner architectural follow-up to #163 — captured in `docs/learnings/2026-05-18-auto-merge-misses-architect-override-completion.md` §"long-term design proposal". Not done.
- **wf-feedback dedup cap-bypass** lives in ADR-0046; implementation plan in PR #166.
- **The 24 review_passed cosmetic projections** — deferred.

## What I didn't do (per directive to focus on durable, not nudges)

- Did not synthetic-nudge stuck papercut tasks individually past the first few. The pattern was load-bearing for early auto-merges; subsequent unsticks are scoped through the retry CLI plan.
- Did not seed the missing periodic-bot workflows (wf-stuck-task-sweep, wf-o11y-regression-scan, etc.). Those need the `wf-rule-corpus-health-workflow` task to merge first, which is wf-feedback: failed.
- Did not cancel or operator-merge stuck PRs to inflate the auto-merge count.

The 5 auto-merges that did fire today represent: 3 real auto-deploy plan tasks (operator-nudged through early bugs that are now fixed), 2 synthetic smokes (validated the pipeline works on fresh trivial tasks). That's an honest characterization.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
