# ADR-0061: role-ui-triage — labelable visual-bug detection via Playwright

- **Status:** accepted
- **Date:** 2026-05-28
- **Related:**
  - ADR-0052 (judgment-role prompts from human-labeled corpora) — the optimizer pattern this corpus plugs into
  - ADR-0053 (agentic judge prompt optimization via workers) — the variant-scoring machinery this role reuses
  - ADR-0056 (operator dashboard as a separate static-served service) — the first surface this role triages
  - ADR-0034 (crystallization pipeline) — same general shape: agent produces structured artifacts, operator labels, optimizer evolves
- **Prompt artifact:** `docs/triage/role-ui-triage.v1.md`
- **Plan:** `docs/plans/2026-05-28-role-ui-triage.md`

## Context

The dashboard (ADR-0056) is in production. It has visible bugs today
(escalation strip dominates the page; BlockingPanel renders fixture
text; useRepoDocs fires a 404 before repo populates). Each of those
was diagnosed in a manual operator-driven loop using Playwright via
ad-hoc Node scripts. That loop works, but:

1. **It does not scale.** Every UI surface Treadmill ships
   (dashboard today, planner UI / validation visualizer / first
   non-Treadmill repo's UI tomorrow) inherits the same per-bug
   operator cost.
2. **It produces no learning signal.** Every diagnosis is throwaway.
   The labels implicit in "did the operator dispatch a fix for this?"
   are not captured anywhere, so the system can never improve at
   doing this itself.
3. **The work is regular enough to automate.** Layout overflows,
   network failures, console errors, accessibility violations, and
   stub-content leaks fit a small closed taxonomy. Most candidate
   findings can be dismissed cheaply by grounding them in a design
   contract; the rest are fix-shaped.

Treadmill already has the infrastructure for "an agent produces
structured outputs that an optimizer evolves over time" — that's
ADR-0052 / ADR-0053 for judge roles. The triage role is structurally
identical: input is a page state, output is a structured decision,
the decisions are labelable, the prompt is a versioned policy.

## Decision

Ship **`role-ui-triage`** — a Treadmill role + workflow that:

1. **Drives a headless browser** (Playwright) against one or more
   target URLs to capture screenshots, DOM snapshots, console events,
   and network events.
2. **Reads design context first** — DESIGN.md, component AGENT.md
   "Recent changes", recent triage findings (last 24 h), open PRs
   on the repo. Required by the prompt, enforced by the order of
   operations.
3. **Produces `TriageFinding` records** — a closed-schema JSON
   shape with three layers (provenance, evidence, decision) plus
   nullable label columns. Each finding fits one of nine closed
   categories or `"other"` (with `"other"`-rate as a taxonomy-gap
   signal).
4. **Makes a deterministic dispatch decision per finding** — first-
   match-wins through an 8-step tree (dedup → infrastructure-out-of-
   scope → design-system-vocabulary check → confidence/severity-by-
   rule). At most 3 dispatched findings per run.
5. **Dispatches a Treadmill plan** for `dispatched` findings,
   `research_only` plans for medium/low-severity cases, and logs
   `suppressed` findings to the corpus for label-driven evolution.

Findings are stored in a new `triage_findings` Postgres table with
the schema defined in this ADR and detailed in the v1 prompt artifact.
Evidence (screenshots, DOM, logs) lives in S3 at
`s3://<corpus_bucket>/triage/runs/<run_id>/<finding_seq>/{screen.png,
console.log,network.log,dom.html}`. Lifecycle events get
`entity_type='triage'` rows on the existing `events` table.

The role runs in two modes via the same workflow:

- **Periodic** — a `SEED_SCHEDULES` entry fires `wf-ui-triage` every
  4 hours against the canonical surfaces. Synthetic-task path
  (ADR-0057) so taskless scheduled dispatch works.
- **On-demand** — `treadmill workflows trigger wf-ui-triage
  --payload '{"target_urls": [...], "on_demand_request": "..."}'`
  for operator-driven probes (the manual workflow we ran throughout
  this conversation, now reproducible).

### Schema

The `TriageFinding` Pydantic model + Postgres table carry:

- **Provenance:** `finding_id`, `run_id`, `created_at`,
  `prompt_version`, `model`, `mode`, `on_demand_request`.
- **Target state:** `target_url`, `viewport_w`/`viewport_h`,
  `git_sha`, `api_git_sha`.
- **Evidence:** S3 URIs for screenshot / console log / network log /
  optional DOM snapshot; an inline `evidence_summary` dict with
  denormalized counts (`console_errors`, `http_4xx`, `http_5xx`,
  `requestfailed`) so labelers can scan without S3 fetches.
- **Detector output:** `category` (closed enum of 9 + `other`),
  `severity` (high/medium/low), `confidence` (high/medium/low),
  `observation` (≤240 char), `evidence_pointer` (cites artifact
  files), `proposed_resolution` (≤900 char; what should happen +
  how to fix, in design-system vocabulary).
- **Dispatcher output:** `dispatch_action` (dispatched / research_only
  / suppressed / escalated_to_operator), `dispatch_reason`,
  `suppression_signal` (closed enum), `parent_finding_id` (collapse
  related findings under one root), `dispatched_plan_id` (FK).
- **Outcome:** `outcome_state`, `outcome_pr_number`,
  `outcome_merged_at`, `recurrence_count` — populated by event
  projection (see "Outcome tracking" below), not a sweeper.
- **Labels** (nullable): `label_is_real_bug`, `label_severity`,
  `label_category`, `label_fix_in_dsl`, `label_dispatch_action`,
  `label_notes`, `labeled_by`, `labeled_at`,
  `label_guidelines_version`. Null labels are tracked per-row as a
  labeling-fatigue signal.

The label columns intentionally live on the same row as the model's
output. The optimizer's training query is one `SELECT` away.
Normalized history (a label_history sidecar table) is deferred to a
future ADR; most scoring needs only the most-recent label.

### Outcome tracking is deterministic event projection

When a dispatched plan's PR merges (or the plan's task is cancelled
/ superseded), the coordination consumer (`services/api/treadmill_api/
coordination/consumer.py`) updates the corresponding
`triage_findings.outcome_state` inside the same projection
transaction as the source event. This mirrors how `task_status` and
`task_mergeability` are already projected — no separate sweeper, no
race window.

### Retention

Triage findings are retained **forever**. The corpus selection for
optimizer training happens at training time via a SQL view; we do
not prune. Storage cost is bounded by the per-finding payload size,
not by retention.

### Calibration via labels, not via launch gate

The v1 prompt ships **not perfect**, but **labelable**. Every
finding it produces is a row in a future training corpus. The first
~30 labels (8 from this conversation's manual triage + the next few
periodic runs) bootstrap the optimizer; subsequent variant proposals
(via the existing `wf-tune-judge-prompts` machinery — generalized to
this role per ADR-0056's pluggable-metrics design) score against the
held-out slice. The bug taxonomy, the dispatch tree, the dedup
thresholds, and the cap of 3 are all parameters the optimizer can
nudge.

## Alternatives considered

### Embed Chromium in the agent image (Tier 2 from initial design)

A worker can take screenshots itself, compare against a baseline,
and verify visually that its fix worked. **Rejected for now.** This
adds ~150 MB to the agent image (Chromium headless-shell), ~30 s to
container cold-start, and complicates the worker_deps surface ADR-0059
just landed. The lighter loop (triage role drives triage; workers
fix; operator visually verifies the merged result) is operationally
sufficient until label data shows otherwise. A future ADR can shift
to closed-loop visual verification when there's evidence the operator-
verify step is a bottleneck.

### Naive prompt without schema

"Tell Claude to look at the dashboard and find bugs." **Rejected.**
Without a schema, every finding is freeform prose. Without labels,
there's no learning signal. Without a closed taxonomy, the model
files everything subjective. This is the failure mode every team
that's tried to automate UI review has hit.

### Operator-only triage (the status quo)

Joe and I have been doing this manually. It works for one surface;
it doesn't scale to N surfaces, and it produces no durable record.
The capability deliverable here is precisely "stop dedicating an
operator to it."

### One role, not two (detector + dispatcher split)

I floated splitting `role-ui-triage-detector` and `role-ui-triage-
dispatcher` for cleaner optimizer signal. **Rejected at v1.** Single
role producing both halves keeps the prompt simpler and the schema
already supports the split (every record has both detector and
dispatcher fields). When label data shows the detector and dispatcher
have separable error modes, we re-prompt without re-shaping the
record.

### Store findings as `events` rows

Reuse the existing append-only events table. **Rejected for the
canonical store.** Labels mutate, and the events table is append-
only. We use `events` for lifecycle pulses
(`finding.detected` / `finding.dispatched` / `finding.suppressed` /
`finding.labeled`) and `triage_findings` for the queryable corpus.

## Consequences

### Good

- **First non-judge role that produces a labelable corpus.** Plugs
  into existing optimizer infrastructure (ADR-0052 / 0053) without
  inventing parallel machinery.
- **Operator workflow scales sub-linearly.** Triage runs cost no
  operator attention except for periodic labeling sessions.
- **First worker-driven Playwright surface.** Establishes the
  pattern for any future role that needs a headless browser
  (visual validation, screenshot-based regression tests, future
  e2e workflows).
- **Bootstrap corpus already exists.** This conversation's manual
  triage produced ~8 labelable examples. The first periodic run
  produces 3 more. Optimizer's seed-001 has ≥10 records on day one.
- **`other`-rate is a built-in taxonomy-gap signal.** When `other`
  exceeds 5 % of dispatched findings, the enum needs expansion;
  the prompt explicitly flags this.

### Bad / trade-offs

- **Agent image grows.** Playwright + chromium-headless-shell add
  ~150 MB to the triage-worker image. We isolate the dependency to
  this role via ADR-0059's per-repo `worker_deps` mechanism: the
  `wf-ui-triage` workflow declares `playwright` and `chromium` as
  required deps; the agent image gets them only when materializing
  for this workflow.
- **New surface to maintain.** A schema, a workflow, a role prompt
  versioned over time, a labeling UI, an outcome-projection hook. ~6
  steps in the rollout plan.
- **Calibration takes labels.** The v1 prompt's quality is hard to
  evaluate without label data, and label data accumulates on
  human-time. We accept this — that's the whole point of the
  cybernetic framing. The first few runs will produce findings the
  operator will partially disagree with; those disagreements are the
  training signal.

### Risks

- **Noise flood.** A miscalibrated prompt could file dozens of
  marginal findings. Mitigated by: closed taxonomy, anti-list, cap of
  3 dispatched per run, dedup against open PRs + recent findings.
  Hard cap is the safety valve.
- **Adversarial findings against design intent.** The role might
  file findings that contradict deliberate design choices. Mitigated
  by required-reading of DESIGN.md + suppression signal
  `design_intent`. The labeler's `label_fix_in_dsl` corrects this
  over time.
- **Lifecycle event projection bugs.** Outcome tracking depends on
  the coordination consumer updating `triage_findings` correctly on
  PR merges. Mitigated by deterministic projection in the same
  transaction as the source event (same pattern as `task_status`,
  audited there).
- **Prompt drift across versions.** Every variant of the prompt is
  versioned (`prompt_version` on every record). The optimizer
  proposes variants; the operator approves before they become the
  default. Drift is auditable.

## References

- Prompt artifact: `docs/triage/role-ui-triage.v1.md`
- Master plan: `docs/plans/2026-05-28-role-ui-triage.md`
- Bootstrap corpus seed (this conversation's manual triage examples):
  `docs/triage/seed-corpus.md` (filled in by Step 1)
- The first triage run that exercises the v1 prompt:
  `/tmp/triage-875c8fba-5ad7-4951-bff9-5ee7c0f891eb/run.json`
  (captured during ADR drafting; will be re-recorded into the corpus
  after Step 1's schema lands)
