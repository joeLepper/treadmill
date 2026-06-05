---
auto_merge: false
status: superseded-diagnosis
---

# Plan: accept-as-is on an open PR routes to merge-eligible, not terminal (root-cause)

- **Status:** DIAGNOSIS CORRECTED 2026-06-05 — see "Corrected root cause" below.
  The original task_status-VIEW-gate approach is **abandoned** (wrong layer).
- **Date:** 2026-06-05
- **Related ADRs:** ADR-0031 (auto-merge cooling-off), ADR-0038/0042 (review/validate
  override), ADR-0048 (task_status surface), ADR-0062 (escalation lifecycle)
- **Related learning:** `docs/learnings/2026-06-04-accept-as-is-terminalizes-task-before-merge.md`
- **Pairs with:** the terminal-gate sweep (shipped, #176) — that's the *detect*
  safety net; this is the *prevent* root-cause fix.

## Corrected root cause (2026-06-05, after reading the merge-core)

The original plan (gate the `task_status` VIEW `done`-clause on an owns-open-PR
predicate) **attacks the wrong layer and is abandoned.** Evidence:

- Auto-merge **arming** (`triggers.maybe_auto_merge_on_mergeable`) and **firing**
  (`triggers.fire_elapsed_auto_merges`) both read `task_mergeability`, **never**
  `task_status`. So a task projecting as terminal `done` does **not** block the
  merge — terminalization is a display/dispatch-gating concern, not a merge
  blocker. Gating the VIEW would not merge a single stranded PR.
- Worse, it would be **net-negative**: the shipped terminal-gate sweep (#176)
  detects orphans by `terminal status × open PR`. Make accept-as-is non-terminal
  and the sweep stops seeing them — we'd remove the safety net without arming the
  merge.

The orphan has **two real mechanisms**, both in the *arming/approval* layer:

- **M1 — never-approved accept-as-is** (the learning's `57598e7a`/#133 case):
  the run ended `responded-without-change`; **wf-review never ran**, so
  `review_decision` is NULL. The architect's `accept-as-is` did **not** set
  `dispatch.review_override` (there was no review to override), so no
  `review.override` event is emitted → `task_mergeability` can **never** reach
  `mergeable` → auto-merge cannot arm, ever.
  **DECIDED 2026-06-05 (Joe): stay operator-merge — "something should review."**
  Accept-as-is must NOT substitute an approval for a PR that was never reviewed;
  it stays a terminal-with-open-PR that the terminal-gate sweep (#176) escalates
  to an operator, who reviews and merges. So M1 takes **no code change** — and
  M2 is consistent with it: a never-reviewed PR is `pending`, not `mergeable`,
  so M2's arming correctly bails. The earlier learning's "proposed rule" (route
  accept-as-is to merge-eligible) is **rejected for the never-reviewed case** on
  this basis; it applies only where an approval signal already exists.

- **M2 — became mergeable via a late github event** (plan success-criterion #3):
  `maybe_auto_merge_on_mergeable` is invoked **only** on `step.completed`
  (`consumer._handle_step`). `_handle_github_event` never calls it. So when a
  task crosses into `mergeable` because a `github.check_run_completed` (CI green),
  `github.pr_synchronize` (clean new HEAD with fresh approvals), `github.pr_conflict`
  resolution, or a late `github.pr_opened` lands **after** the last step,
  arming never re-fires → the green, approved PR strands. **Safe, self-contained
  fix; implemented here.**

### The fix actually built (M2)

Factor the post-`task_id` body of `maybe_auto_merge_on_mergeable` into a shared
`_arm_auto_merge_for_task(session, redis, task_id)`. Keep the existing
step-based wrapper. Add a github-based entrypoint
`maybe_auto_merge_on_github_event(session, redis, *, repo, pr_number)` that
resolves `task_id` via `task_prs (repo, pr_number)` and calls the shared body.
Call it from `_handle_github_event` for `pr_opened`, `pr_synchronize`,
`check_run_completed` (skip when `pr_number` is None), and `pr_conflict`.

**Why it's safe:** arming only ever writes the deadline when `task_mergeability`
already reads `mergeable` (validate=pass, review=approved, CI ok, no conflict),
and `fire_elapsed_auto_merges` re-verifies mergeability before the PUT. The new
call cannot merge anything that isn't already fully mergeable — it only closes
the timing gap where the final transition arrived on a github event instead of a
step. No VIEW change; the sweep stays intact as the backstop.

## Goal

Stop an architect `accept-as-is` verdict from terminalizing a task **whose PR is
still open**. Today accept-as-is projects the task to a terminal `done` in the
`task_status` VIEW; terminal status gates further dispatch, so nothing remains to
merge the (green, approved) PR — it orphans, and anything blocked on
`task.<id>.pr_merged` waits forever. Observed 3× on 2026-06-04 (PRs #176, #185 +
others had to be operator-merged by hand). The fix: when accept-as-is would
terminalize a task that **owns an open, unmerged, un-closed PR**, project a
**merge-eligible** status instead, so the ADR-0031 auto-merge path acts on it,
the PR merges, and `pr_merged` then yields the true terminal `done`.

## Success criteria

1. The `task_status` VIEW projects a task that reached `done` **only via the
   accept-as-is / override path** as a **merge-eligible** status (not terminal)
   while it **owns an open PR** — defined exactly as the terminal-gate sweep's
   "owns an open PR" predicate: a `github.pr_opened` exists and there is no
   `github.pr_merged` and no `github.pr_closed` for the task.
2. `github.pr_merged` (or `task.cancelled` / `task.superseded`) still yields the
   true terminal status — only those terminalize a task that owns an open PR.
3. The ADR-0031 auto-merge eligibility arms for a green accept-as-is PR so it
   auto-merges without an operator (the learning's auto-remediation candidate),
   instead of orphaning — i.e. the merge-eligible projection feeds the existing
   auto-merge trigger path.
4. No regression to the *non*-accept-as-is terminal paths (a normally-merged
   task, a cancelled/superseded task, a gate-failed task) — the VIEW precedence
   for those is unchanged.
5. Tests pin: accept-as-is + open PR → merge-eligible (NOT done); + pr_merged →
   done; cancelled/superseded → terminal; normal merge → done. Plus an
   end-to-end: accept-as-is on an open PR auto-merges instead of orphaning.

## Constraints / scope

### In scope
- A new Alembic migration evolving the `task_status` VIEW (the
  `0002 → 0017 → 20260520_0500` chain) — `CREATE OR REPLACE VIEW` with the
  accept-as-is-done clause gated on the open-PR predicate, plus the downgrade.
- The auto-merge trigger wiring (ADR-0031) if it keys on the task being in a
  specific status — ensure the new merge-eligible status feeds it.
- `services/api/tests/` — task_status VIEW tests + an auto-merge end-to-end.
- AGENT.md if it documents the task_status states.

### Out of scope
- The terminal-gate sweep (already ships the detect side; this is prevent).
- The `task_mergeability` VIEW (the override → mergeable signal is correct as-is;
  the bug is the task terminalizing, not the mergeability calc).
- Backfilling already-orphaned tasks (the sweep + operator-merge handle those).

### Budget
One change. **`auto_merge: false`** — this is the core lifecycle status
projection (fleet-wide blast radius). The orchestrator verifies it against a
real Postgres before merge.

## Execution note (why hand-implemented, DB-verified)

The `task_status` VIEW is a priority-ordered CASE; its tests require a live
Postgres. The worker validation sandbox cannot reliably run a DB-backed gate, and
the blast radius (every task's status) is too high for a grep-only gate. So the
orchestrator hand-implements this and verifies against the local Postgres
(`docker exec treadmill-postgres psql`) — the migration applies cleanly, the
VIEW recreates, and the new + existing task_status tests pass — before opening
the PR. The terminal-gate sweep means there is **no urgency**: orphans are caught
today, so this can be done carefully rather than rushed.

## Sequence of work (hand-implemented)

1. **STUDY** the full `task_status` VIEW (latest migration
   `20260520_0500_task_status_merged_precedence.py`) — locate the clause that
   yields the terminal `done` for an accept-as-is/override-accepted task (vs. the
   `pr_merged`, cancelled, blocked, and in-flight clauses), and the precedence
   ordering. Read the terminal-gate sweep's `_ORPHANED_PR_SQL` for the exact
   open-PR predicate to reuse. Read the ADR-0031 auto-merge trigger to see what
   status/event it keys on.
2. **BUILD** a migration that `CREATE OR REPLACE VIEW task_status` with the
   accept-as-is-done clause gated: `... AND NOT <owns-open-PR>` so it falls
   through to a merge-eligible projection while the PR is open; `pr_merged`
   precedence unchanged. Wire the merge-eligible status into the auto-merge
   trigger if needed. Provide the downgrade (restore the prior VIEW).
3. **VERIFY** against the local Postgres: migration up/down applies; the VIEW
   recreates; new + existing `task_status` tests pass; an end-to-end shows an
   accept-as-is open PR auto-merging.

## Verification

- Local Postgres: `alembic upgrade head` applies; `\d+ task_status` shows the
  gated clause; `cd services/api && uv run pytest` for the task_status +
  auto-merge tests passes (run locally where Postgres is available, NOT a
  sandbox gate).
- Post-merge: a real accept-as-is verdict on an open PR auto-merges without an
  operator + without a terminal-gate-sweep escalation firing.

## Key risk

**Don't break the status projection fleet-wide.** The change must be a narrow
gate on exactly the accept-as-is-done clause, leaving cancelled/superseded/blocked
/pr_merged/in-flight precedence identical. DB-verified before merge; the
terminal-gate sweep backstops any residual orphan. `auto_merge: false` so the
orchestrator reviews + merges deliberately.
