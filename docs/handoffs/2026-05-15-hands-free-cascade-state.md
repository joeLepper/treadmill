# Hands-free cascade — state as of 2026-05-15 16:18 UTC

Working state for the multi-hour push to fully-converged hands-free driving. Captures landed work, remaining gaps surfaced by the smoke, and the prioritized next patches.

## Landed this push (commits on main)

| PR/commit | What | Why it matters |
|---|---|---|
| #63 | Phase 4 prereq snapshot handoff | Gate; names all five ADR-0031 prereqs |
| #64 | `maybe_auto_merge_on_mergeable` + consumer wiring + migration 0012 (`plans.auto_merge` column) | The auto-merge cooling-off trigger itself |
| #65 | Dedup namespace `auto-merge=<task_id>` + `task.<id>.auto_merged` event | Idempotency + downstream-observability for auto-merge |
| #66 | Hand-impl: `parse_plan_doc_frontmatter()` + StrictBool field + SKILL.md docs | Author bot couldn't converge after 4 rounds; operator finished |
| #67 | Wire `parse_plan_doc_frontmatter()` into the three Plan-creation paths | #66 added the function but didn't call it; without this `Plan.auto_merge` was always NULL |
| #68 | `PlanFrontmatter.extra="ignore"` | #66's strict frontmatter rejected every plan with conventional `status:` / `trigger:` fields |
| `33d9f4f` | `validation_runtime`: capture stdout, not just stderr | We were flying blind through 3 parser re-fires because pytest writes failures to stdout |
| `14ce8d4` | Revert role-code-author to haiku | The sonnet bump was a red herring; failures were harness gaps |
| `8b62a8d` | Trim2 parser validation script: fix cd-relative path | Author was failing on a script bug, not a code bug |
| `0ab8033` | Learning: author-side fail has no remediation path | task #121 catches bad pushes but the silent-stall has no recovery |

## Smoke 1 + 2 results (16:00 UTC)

Submitted two plans:

- **Smoke 1** (plan `50d883bf`, `auto_merge=NULL`): wf-author → PR #69 → wf-review → verdict `comment` / `needs-more-info`. **Stuck.**
- **Smoke 2** (plan `323b08dd`, `auto_merge=false`): wf-author → PR #70 → wf-review → same verdict. **Stuck.**

The opt-out wiring is verified at the data layer (`Plan.auto_merge=false` persisted from the frontmatter) but neither smoke reached the auto-merge predicate because no PR ever got to `review_decision='approved'`.

## Open gaps blocking convergence

Ordered by what unblocks the most downstream work:

### Gap A: reviewer verdict mapping is too narrow

`role-reviewer` returns `comment` / `needs-more-info` for a trivial, clean PR. Neither verdict routes anywhere in the dispatch table — they're a black hole:

- Not `approved` → `maybe_auto_merge_on_mergeable` short-circuits.
- Not `request_changes` → `wf-feedback` doesn't dispatch.
- Not `uncertain` → `wf-architecture-resolve` doesn't dispatch.

So the task sits in `wf-review.completed.<verdict>` forever. **This is the primary blocker.**

Two ways to fix, complementary:
1. **role-reviewer side**: prompt the role to emit `approved` when nothing needs changing. Today it conservatively emits `comment` because "I didn't add changes" feels weaker than "this is good". Re-tune the prompt to bias toward `approved` when no issues are found.
2. **Dispatcher side**: treat `comment` as `approved` (it ~is, semantically) OR route it explicitly to `wf-feedback` with the comment body as the prompt. Probably do this even if the prompt is fixed — defensive.

Related: task #114 (delete VERDICT regex tourniquet from review.py) hints at known fragility here.

### Gap B: dual-identity for operator-approves-bot is unfinished

GitHub rejects "Can not approve your own pull request" when the same identity that opened the PR (the bot) tries to approve, OR when the operator's `gh` identity matches. Task #108 ("Dual-identity for bot PRs") is marked completed but the path "operator manually approves a bot PR" wasn't covered — both PRs were authored under `joeLepper` and I can't approve under the same identity.

This isn't blocking Gap A (the autonomous path doesn't need operator approval), but it blocks the operator-bypass workaround for stuck reviews.

### Gap C: verdict-comment auto-merge predicate consideration

When (and only when) Gap A is fixed and reviewer emits `approved`, we still need `wf-validate.decision='pass'` for auto-merge. We never saw wf-validate dispatch in either smoke. Need to verify wf-validate dispatches after wf-review (likely chained), or fix that link.

## What's been ruled out

- **Model quality.** Sonnet bump didn't address the parser issues — they were harness gaps. Reverted.
- **`task_validations` snapshot** as a generalized concern. It's real but in-scope tightening of `description` field has worked; the script edit pattern is fine for surgical fixes.
- **Validation log truncation.** Fixed via stdout capture.
- **Frontmatter strictness.** Fixed via `extra="ignore"`.

## Next concrete patches (in order)

1. **Patch role-reviewer prompt** to bias toward `approved` when no actionable issues found. Update `services/api/treadmill_api/starters.py` role definition. Add a starters test that the prompt mentions the bias. Commit, push, redeploy. Drives Gap A.
2. **Resubmit smoke 1**, observe whether wf-review now emits `approved`. If yes, wf-validate should dispatch and auto-merge should fire 30s after mergeable.
3. **Fallback if (1) doesn't converge**: dispatcher-side patch that treats `comment` as `approved` OR explicitly routes it to `wf-feedback`.

Smoke 2 (opt-out) doesn't need any of this to validate — once smoke 1 fires, smoke 2 should reach the same predicate state and the trigger should skip per `plan.auto_merge=false`.

## Outstanding monitor / process commitments

- 60s heartbeat pattern is now the standard for any in-flight worker (see `feedback_active_supervision_not_optimistic_wait`).
- Long-haul mode: no proposing to defer; drive the work (see `feedback_long_haul_no_session_breaks`).
- Held plans: ADR-0034 (learnings crystallization), ADR-0035 (scheduler), o11y trim — all drafted + committed, all NOT dispatched, awaiting hands-free convergence.
