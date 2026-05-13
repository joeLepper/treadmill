---
status: completed
trigger: Operator scoped 2026-05-13 — implement the post-loop-hardening work
  (task #108 + ADR-0028 plan + ADR-0027 plan + smoke) in-session rather than
  via the Treadmill loop, since the loop itself depends on #108 to make it
  past wf-review.
parents:
  - docs/adrs/0027-structured-json-for-review-output.md
  - docs/adrs/0028-db-authoritative-workflow-configs.md
  - docs/handoffs/2026-05-12-loop-hardening-and-first-smoke.md
completed_at: 2026-05-13
---

## Post-mortem (landed 2026-05-13)

All four phases shipped in-session. Phase 4 smoke validated the
full chain end-to-end against the personal deployment via PR #17
(plan-merge trigger) → o11y plan submitted → task 1 (Wire OTel SDK)
→ wf-author opened PR #18 → wf-review landed cleanly via the new
parser + transport.

**The full chain confirmed working:**

| Validation | Evidence |
| --- | --- |
| Plan-merge trigger (ADR-0021) | PR #17 merge → 5 o11y tasks created |
| #108 path 1 (`gh pr comment`) | PR #18 has 1 comment; no `gh pr review` call; no same-author block |
| ADR-0027 JSON envelope | `wf-review.step.completed` carries `payload.rationale` (only possible via JSON path — regex tourniquet returns None) |
| ADR-0027 fence-strip (Q27.c) | PR #18 comment body has the `## Treadmill review verdict: approve` header but no `\`\`\`json` block |
| ADR-0028 auto-seed (Q28.a) | New JSON-envelope prompt loaded into running deployment via `seed_starters_if_empty` on first API startup |
| ADR-0028 PATCH endpoint | Confirmed pre-smoke via `treadmill role update role-reviewer` (smoke-test edit landed as v2) |
| ADR-0028 `--reset-prompts-from-code` | Confirmed pre-smoke: 8 roles reset, v3 audit row appended with reset-origin note |
| ADR-0026 dispatch dedup | Exactly 1 row in `workflow_dispatch_dedup` for `wf-review:joeLepper/treadmill:pr=18,sha=fb647952…` |
| ADR-0027 drift-warning surface | Zero `review.json_parse_failed` warnings — JSON path holds on the first run |
| `wf-feedback` self-trigger (#108 wrinkle) | Not exercised this smoke (verdict was `approve`, not `changes_requested`). Tracked: validate on a future smoke where the reviewer pushes changes. |

**Commits this session (10 total):**

```
117d520  Phase 0   ADRs Accepted + plans Active + sequencing doc
65022aa  Phase 1   #108 path 1 (gh pr comment + decision-based self-trigger)
e942251  Phase 2a  role_versions table + RoleVersion model + alembic backfill
acaec57  Phase 2b  PATCH + GET versions endpoints + v1-on-create
bef0169  Phase 2c  seed --reset-prompts-from-code recovery flag
11474ad  Phase 2d  treadmill role show/update/versions CLI
e34144c  Phase 2e  auto-seed on first API startup (Q28.a ii)
b87d340  Phase 2f  operator runbook + plan post-mortem
fa0f82c  Phase 3   JSON envelope parser + role-reviewer prompt rewrite
b6ae461  Phase 3   plan post-mortem
b7c21b4  Phase 4   smoke-trigger PR #17 (squashed merge)
```

**Test coverage:** 480 API non-integration + 41 worker disposition +
32 starter + 28 CLI = 581 unit/contract tests green. Integration
tests (10 new for PATCH+versions, 3 new for auto-seed, 9 new for
CLI role subcommands) ready under `TREADMILL_INTEGRATION=1`.

**Deferred items (per resolved Open Qs):**

- Tourniquet regex deletion: Q27.a's bar is 10 consecutive clean
  runs. This smoke is run 1. Follow-up after the bar is met.
- `treadmill role rollback`: Q28.b deferred until a forcing function
  arises. Workaround documented in the runbook.
- `wf-feedback` self-trigger validation: needs a smoke where the
  reviewer returns `changes_requested`. Tracked for next smoke.
- Task #109 (Treadmill as GitHub App): explicitly future cleanup.

**What surprised us:**

- ADR-0028's plan assumed a `role_versions` table existed — it
  didn't. Phase 2a added the migration + ORM model as the
  foundation; the plan absorbed the discovery without a redesign.
- The smoke's first wf-review verdict was `approve` (not the
  `request_changes` we half-expected from a one-line PR). The
  reviewer's prompt frames substantive review behavior + the
  diff was actually clean.

# Plan: in-session sequencing for #108 + ADR-0028 + ADR-0027 + smoke

Thin coordination layer over the two existing detail plans
(`2026-05-13-db-authoritative-configs.md`,
`2026-05-13-structured-review-envelope.md`) plus task #108. Documents
the order, the decisions made up-front, and the cross-plan
dependencies the detail plans don't carry themselves.

## Decisions locked at planning time

### Task #108: path 1 (comment-based reviewer)

**Decision:** Replace `gh pr review` with `gh pr comment` in the
review-kind disposition handler. Bunkhouse precedent. Confirmed by
ADR audit: the mergeability VIEW
(`alembic/versions/0006_task_mergeability_view.py:95-106`) reads
`review.decision` from `workflow_run_steps.output->>'decision'`, not
from GitHub's `pr_review_submitted` event. Switching the GitHub call
preserves mergeability correctness.

Path 2 (separate PAT identity) explicitly rejected.

Path 3 (Treadmill as a real GitHub App) → task #109 (future cleanup).

### `wf-feedback` trigger shape

Switching to `gh pr comment` breaks the existing
`pr_review_submitted → wf-feedback` trigger
(`alembic/versions/0007_seed_event_triggers.py:78`) for Treadmill's
*own* self-reviews. Three options surfaced during planning:

* **(a) Step-output decision trigger.** Extend `triggers.py` to fire
  `wf-feedback` when a `wf-review` `step.completed` event arrives
  with `decision='changes_requested'`. Uses Treadmill's own
  envelope (same shape the mergeability VIEW already reads).
  ADR-0026's dedup table already keys `wf-feedback:review=<id>`, so
  re-fire protection is automatic.
* **(b) Artifact-as-trigger.** Add `Artifact(kind="pr_review_submitted",
  value=verdict)` to the review handler's output, have `triggers.py`
  read artifacts as a trigger source. Requires extending the
  `Artifact.kind` `Literal` (a Pydantic contract change) and stretches
  the artifact semantic — artifacts are "the product of a step," not
  "a signal that fires another workflow."
* **(c) No auto-trigger.** Self-reviews land in the mergeability
  VIEW as `blocked-on-review` but don't auto-loop until a human nudges.

**Decision: option (a).** Cleanest fit with how the mergeability
VIEW already reads the envelope; no Pydantic contract change; no
new semantic. Option (b) revisited if a future feature wants other
runtime artifacts to act as triggers (then the framing is "introduce
an event-emitting artifacts mechanism").

### Plan independence: ADR-0027 and ADR-0028 are not sequentially coupled

ADR audit confirmed that ADR-0027 task 2 (role-reviewer prompt
rewrite) does *not* require ADR-0028's CLI — the plan doc explicitly
accommodates the legacy `seed-starters --reset-prompts-from-code`
flow. The two plans can land in either order or in parallel. For
in-session sequential execution we pick an order below; the
constraint is operator preference, not a technical dependency.

## Sequence

```
Phase 0 ── Resolve Open Qs Q27.a-d + Q28.a-e  (operator decision, in-chat)
   │
   ├──► Phase 1 ── Task #108 (gh pr comment + decision-based wf-feedback
   │                 trigger). Does NOT depend on Phase 0; can land in parallel.
   │
   ├──► Phase 2 ── ADR-0028 plan (5 tasks). Blocked by Phase 0.
   │
   ├──► Phase 3 ── ADR-0027 plan (3 tasks). Blocked by Phase 0.
   │
   └──► Phase 4 ── End-to-end smoke. Blocked by Phases 1 + 2 + 3.
```

**Why this order:** #108 has the smallest blast radius and is
strictly required for any smoke to land past wf-review. ADR-0028
before ADR-0027 because the prompt rewrite in ADR-0027 task 2 is
slightly more elegant after the role-update CLI lands (one CLI
invocation vs. code-edit + seed dance), and the runbook in
ADR-0028's task 5 references the prompt-edit pattern. The two could
swap order at the cost of a less polished ADR-0027 task 2; the
locked sequence is the cleaner path.

## Cross-plan touchpoints

| Plan | File | Touches | Notes |
| --- | --- | --- | --- |
| #108 | `workers/agent/treadmill_agent/runner_dispositions/review.py` | `handle()` body | Same file ADR-0027 task 1 modifies. Sequence #108 before ADR-0027 task 1 — or rebase. |
| #108 | `services/api/treadmill_api/coordination/triggers.py` | new trigger branch | Standalone; no overlap with other plans. |
| ADR-0028 | `services/api/treadmill_api/starters.py` | `seed()` body, prompt content | ADR-0027 task 2 also touches `starters.py` (prompt edit). Sequence ADR-0028 first; ADR-0027 task 2 then uses the new CLI. |
| ADR-0027 task 1 | `runner_dispositions/review.py` | adds JSON parser ahead of regex | Lands cleanly after #108's `gh pr review → gh pr comment` swap. |

## Phase 0 resolutions (locked 2026-05-13)

Operator confirmed all nine proposed resolutions in-chat. Both
detail plans' frontmatter has flipped `status: drafting → active`;
both ADRs are now `Accepted`. Full rationale captured in the ADRs
themselves (`§"Resolved decisions"`); summary table here for
discoverability.

| Q | Decision |
| --- | --- |
| Q27.a | **10 consecutive clean runs** before tourniquet deletion |
| Q27.b | `rationale: max_length=4000` |
| Q27.c | Strip the JSON fence **without marker** |
| Q27.d | Parser always runs (incl. dry-run); skip only `gh pr review` itself |
| Q28.a | **(ii) auto-seed on first API startup** with `SELECT … FOR UPDATE` on `alembic_version` |
| Q28.b | CLI: `show + update + versions`; **defer rollback** |
| Q28.c | **Keep `starters.py` in repo** at current location |
| Q28.d | Add `notes` + `pr_url` columns to `role_versions` |
| Q28.e | **Roles only** — workflows stay code-driven |

## What this plan does NOT do

* Implement task #109 (GitHub App). Tracked as future cleanup; no
  in-session work.
* Re-decide path-1 for #108 if it breaks. The smoke in Phase 4 is
  the verification; if it fails, this plan reopens with a post-mortem.
* Hand off to the Treadmill loop. The operator scoped this as
  in-session implementation per "until all of this gets done we're
  implementing it ourselves in this session." Re-firing through the
  loop is deferred until the smoke confirms #108 holds.

## Pending tasks tracked in TaskList

| ID | Phase | Subject |
| --- | --- | --- |
| (Phase 0) | 0 | Resolve Open Qs Q27.a-d + Q28.a-e |
| #108 | 1 | Replace `gh pr review` with `gh pr comment` + decision-based wf-feedback trigger |
| (Phase 2) | 2 | Execute ADR-0028 plan in-session |
| (Phase 3) | 3 | Execute ADR-0027 plan in-session |
| (Phase 4) | 4 | End-to-end smoke validation |
| #109 | (future) | Treadmill as a real GitHub App — no in-session work |
