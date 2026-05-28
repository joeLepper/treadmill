# Plan: role-ui-triage rollout (ADR-0061)

- **Status:** active
- **Date:** 2026-05-28
- **Related ADRs:** ADR-0061 (the architectural decision), ADR-0052
  / ADR-0053 (optimizer machinery this plugs into), ADR-0056
  (dashboard — the first surface), ADR-0057 (synthetic-task path —
  used by periodic schedule), ADR-0059 (worker_deps — how Playwright
  reaches the triage worker), ADR-0060 (sidecar HTTPS proxy — egress
  scoping for the triage worker)

## Goal

Ship `role-ui-triage` into production: a periodic + on-demand triage
workflow that produces labelable `TriageFinding` records and
dispatches plans for the fixes. The success bar is **the first
periodic run produces ≥1 dispatched finding + ≥1 suppressed finding
without operator intervention, and the records are queryable for
labeling.**

## Success criteria

- A `wf-ui-triage` workflow exists and is invokable via
  `treadmill workflows trigger`.
- A periodic schedule fires it every 4 h against the dashboard's
  Overview and TaskDetail pages.
- Each invocation produces structured `TriageFinding` rows in
  Postgres with S3-backed evidence.
- Dispatched findings spawn real plans via `treadmill plan submit`,
  with the screenshot + console excerpts carried in the plan's
  required reading.
- Outcome state on `triage_findings` updates deterministically when
  the dispatched plan's PR merges (or task cancels / supersedes).
- A labeling surface exists that lets the operator flip through
  findings and apply `label_is_real_bug` / `label_severity` /
  `label_dispatch_action` / `label_fix_in_dsl`.
- The first periodic run after rollout produces a `run.json`
  matching the schema, indistinguishable from the manual run in
  `/tmp/triage-875c8fba-...`.

## Constraints / scope

### In scope
- New `triage_findings` Postgres table + migration.
- New `routers/triage/` (or extension of dashboard router) for the
  S3 upload + label endpoints.
- New `role-ui-triage` seeded in `starters.py` with the v1 prompt.
- New `wf-ui-triage` workflow.
- Agent-image extension OR sidecar for Playwright + chromium-
  headless-shell, scoped via ADR-0059 worker_deps.
- `SEED_SCHEDULES` entry for the periodic invocation.
- Coordination-consumer hook for outcome projection.
- A simple labeling UI (per the labeling-UI-workflow memory pin —
  flip-through thumbs-up/down + side-by-side evidence).

### Out of scope (explicit follow-ups)
- Variant optimization. We bootstrap the corpus; running
  `wf-tune-judge-prompts` (or a fork) against it is a follow-up ADR
  once we have ≥30 labels.
- Closed-loop visual verification (Tier 2 from the design
  conversation). Deferred to a future ADR.
- Triage of non-Treadmill surfaces (RAMJAC UI, planner UI).
  Same role, same workflow, just a different target URL —
  out of scope until the dashboard loop is proven.
- A separate detector / dispatcher split. Single role at v1; we
  split when label data shows it's worth it.

## Sequence of work

The six steps below are sized to be individually dispatchable as
Treadmill plans. Step 1 blocks Steps 2-6. Steps 2/3/4/6 can
parallelize. Step 5 depends on Step 3. Per the dashboard PR-B
pattern (Waves 1-3 today), each step is its own plan doc + PR with
`auto_merge: false` while the sibling RAMJAC session is live.

### Step 1 — schema + storage (blocks everything)

Land the `triage_findings` table, Pydantic model, repository, S3
prefix conventions, and a one-time backfill of the manual triage
records this conversation produced (as the seed corpus).

**Scope.files:**
- `services/api/treadmill_api/models/triage_finding.py` (new)
- `services/api/alembic/versions/<sha>_triage_findings.py` (new)
- `services/api/treadmill_api/triage_store.py` (new repository
  with `insert_finding`, `update_outcome`, `record_label`)
- `services/api/tests/test_triage_store.py` (new)
- `services/api/AGENT.md` (Recent changes entry)
- `docs/triage/seed-corpus.md` (new — the 8 manual examples from
  this conversation, formatted as `TriageFinding` JSON so they can
  be loaded into the table once it exists)

**Validation:** the new tests pass; the API factory imports;
alembic upgrade runs against a fresh DB; the model round-trips JSON.

### Step 2 — triage worker image with Playwright

Extend the agent image (or build a sidecar image) so the
`wf-ui-triage` workflow has Playwright + chromium-headless-shell +
the `/opt/triage/probe.mjs` and `/opt/triage/walk.mjs` scripts at
known paths.

This step coordinates with ADR-0059. The workflow declares
`worker_deps: ["playwright", "chromium-headless-shell"]`; the per-
workflow materialization installs them; other workers don't pay
the bloat cost.

**Scope.files:**
- `workers/agent/Dockerfile` OR `workers/triage/Dockerfile` (new
  sidecar) — pick during plan drafting based on ADR-0059's
  preferred extension pattern.
- `workers/agent/scripts/triage/probe.mjs` (new — port from
  `/tmp/playwright-probe/probe.mjs` with the artifact paths the
  schema expects).
- `workers/agent/scripts/triage/walk.mjs` (new).
- `tools/local-adapter/tests/test_image_build.py` (extend) — assert
  Playwright surfaces in the resulting image.
- `tools/local-adapter/treadmill_local/runtime.py` (extend
  `_ensure_images_built` to include the new image if sidecar).
- `tools/local-adapter/AGENT.md` (Recent changes entry).

**Validation:** the image builds; `docker run <image> npx playwright
--version` returns a version; the scripts are at the documented paths.

### Step 3 — workflow + role seed (the role goes live)

`starters.py` gains `role-ui-triage` with the v1 prompt loaded from
`docs/triage/role-ui-triage.v1.md` and a `wf-ui-triage` workflow that
runs the role once per invocation.

**Scope.files:**
- `services/api/treadmill_api/starters.py` (extend) — `role-ui-
  triage` seed; `wf-ui-triage` workflow seed (one author step,
  invokes `role-ui-triage`).
- `services/api/tests/test_starters.py` (extend) — assert
  `role-ui-triage` is seeded with the v1 prompt verbatim;
  `wf-ui-triage` is seeded.
- `docs/triage/role-ui-triage.v1.md` (already in repo from this
  PR; starters.py loads it).
- `services/api/AGENT.md` (Recent changes entry).

**Validation:** new tests pass; a manual invocation via
`treadmill workflows trigger wf-ui-triage --payload '{...}'`
returns a `run_id`.

### Step 4 — outcome projection

Coordination consumer hook: when a `pr_merged` (or `task.cancelled`,
`task.superseded`) event fires, UPDATE `triage_findings` whose
`dispatched_plan_id` matches the source's `plan_id`, setting
`outcome_state` + `outcome_pr_number` + `outcome_merged_at` in the
same transaction as the event projection.

**Scope.files:**
- `services/api/treadmill_api/coordination/consumer.py` (extend)
- `services/api/tests/test_consumer_triage_outcome.py` (new)
- `services/api/AGENT.md` (Recent changes entry)

**Validation:** new test passes; existing consumer tests stay green;
inserting a `pr_merged` event for a task whose `plan_id` matches a
dispatched finding updates that finding's outcome row.

### Step 5 — periodic schedule + operator trigger (depends on Step 3)

Wire `wf-ui-triage` into the scheduled-dispatch path (ADR-0057
synthetic task) and the operator-trigger surface so periodic and
on-demand modes both work.

**Scope.files:**
- `services/api/treadmill_api/seed/schedules.py` (extend) — new
  `SEED_SCHEDULES` entry, every 4 h, payload carries the canonical
  dashboard URLs.
- `services/api/tests/test_seed_schedules.py` (extend) — count +
  workflow-id assertions update.
- `services/api/AGENT.md` (Recent changes entry).

**Validation:** new tests pass; `treadmill schedules list` shows
the new entry; a periodic tick fires the workflow.

### Step 6 — labeling UI (parallelizable with Steps 2-4)

A flip-through labeling page (per the labeling-UI-workflow memory).
Simplest possible: read unlabeled findings, show side-by-side
(screenshot + observation + proposed_resolution), accept four
labels + free-text notes, persist via `routers/triage/labels.py`.

**Scope.files:**
- `services/api/treadmill_api/routers/triage/labels.py` (new —
  POST `/api/v1/triage/findings/:id/label`, GET
  `/api/v1/triage/findings?label_is_real_bug=null`)
- `services/dashboard/src/pages/TriageLabeling.tsx` (new)
- `services/dashboard/src/api/queries.ts` (extend with
  `useUnlabeledFindings`, `useLabelFinding`)
- `services/dashboard/src/App.tsx` (route)
- `services/dashboard/AGENT.md` + `services/api/AGENT.md` entries

**Validation:** new tests pass; rendering the labeling page shows a
finding from a real `triage_findings` row; submitting a label
persists.

## Dispatch order

- **Dispatch Step 1 immediately on merge of this plan.** It blocks
  everything else.
- **Once Step 1 lands**, dispatch Steps 2, 3, 4, 6 in parallel (no
  shared files; Step 2 may be paced separately depending on whether
  it builds a new image — slow CI).
- **Once Step 3 lands**, dispatch Step 5.
- **After Step 5 merges**, the first periodic run fires. The
  operator labels the run's findings via the Step 6 UI. The corpus
  is bootstrapped.

## Operating constraints (carried from current convention)

- `auto_merge: false` on all dispatched plans while the sibling
  RAMJAC session is live ([[feedback-concurrent-orchestrators]]).
- Bake `services/api/AGENT.md` (or the relevant component AGENT.md)
  into every step's scope ([[feedback-architect-overrules-doc-rule]]).
- Scoped validation only ([[feedback-worker-validation-script-scope]]).
- Check wf-review verdict in DB before merging
  ([[feedback-check-wf-review-verdict-before-merge]]).

## Risks / unknowns

- **The v1 prompt may produce noise.** We accept this; the
  labeling step is the corrective. If noise is bad enough to stall
  operator review, we pause periodic runs (`schedules` UPDATE) and
  patch the prompt manually before the optimizer is wired.
- **Outcome projection might drift if Treadmill's event vocabulary
  changes.** Mitigated by testing against the existing `pr_merged`
  projection — same hook point, same transaction shape.
- **Image-bloat trade-off.** If ADR-0059's per-workflow materialization
  isn't ready to scope the Playwright dep, we either inline it in the
  agent image (worse) or block until it is (slower). Step 2's plan
  doc audits this before deciding.

## Decisions captured during execution
*(populated as we go)*

## Post-mortem
*(filled on completion of all 6 steps)*
