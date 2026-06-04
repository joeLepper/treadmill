# ADR-0070: Pre-labeled review queues — generalized cybernetic label surface

- **Status:** proposed
- **Date:** 2026-06-04
- **Related:**
  - ADR-0052 (judgment-role prompts from human-labeled corpora) — the corpus shape this surfaces
  - ADR-0053 (agentic judge prompt optimization via workers) — the optimizer that consumes these corpora
  - ADR-0056 (operator dashboard auto-discovery) — the mount pattern this rides on
  - ADR-0061 (role-ui-triage — labelable visual-bug detection) — the precedent table shape; this ADR generalizes its labeling layer
  - ADR-0027 (structured JSON envelopes) — the per-row recommendation contract

## Context

Treadmill keeps growing review surfaces where a human's job is to sanity-check
an LLM's proposal, not to label from scratch:

- The architect / validator / reviewer **gold corpora** for ADR-0052 prompt
  tuning — `docs/analysis/architect-gold-labels.json` and
  `docs/analysis/validator-corpus.jsonl` were both built by hand-rolled
  scripts and one-off UIs.
- The **UI-triage findings** flow from ADR-0061 — `TriageFindingRow` already
  has detector output + nullable operator `label_*` columns and a labeling UI;
  this is the precedent we generalize.
- Speculative future surfaces: **borderline auto-merge** decisions,
  **plan pre-dispatch** review, **DSPy variant** approve/reject,
  **escalation triage** (cancel / retry / fix), **crystallization rule**
  promotion.

Each surface today is bespoke. We rebuild the queue, the viewer, the keyboard
shortcuts, the label-write path, and the export-to-corpus path per use case.
And critically, none of them close the **cybernetic loop**: as the operator
labels, the LLM that proposed the labels should get measurably better at
proposing — and the operator workload should shrink toward zero. Today that
loop only exists conceptually; ADR-0061 sketched it for triage, but the wiring
to a corpus + optimizer remains open.

The shape repeats across every use case:

```
LLM-as-judge proposes → review queue → flip-through UI → label sink → corpus
                                              ↑
                                      cybernetic feedback
                                      (label-accuracy fraction scores
                                       the judge's prompt; Wave 4 / DSPy
                                       tunes against it)
```

The base case for the operator is **one keystroke** to confirm the
recommendation; the corrective case is **two keystrokes plus a reason note**
to override.

## Decision

Ship **pre-labeled review queues** as a Treadmill primitive: a documented
shape that every "operator sanity-checks LLM" surface follows, with
auto-mounted dashboard routes + viewers per kind, and one labeled-corpus
sink per kind feeding the optimizer.

### Per-kind table shape (no JSONB outside committed sites)

Each review-queue kind gets its own Postgres table. JSONB is not introduced
as a payload column — the architecture commits JSONB only to the small set
of explicit sites (`events.payload`, `schedules.payload_template`,
`workflow_run_steps.output`, `triage_findings.evidence_summary`). Per-kind
tables follow the **`TriageFindingRow`** shape from ADR-0061, with six
mandatory layers:

1. **Provenance** — `id` (UUID, server-default `gen_random_uuid()`),
   `created_at` (TIMESTAMP server-default `now()`), `source_run_id` /
   `source_event_id` / `source_url` / `source_pr_number` as the
   per-kind anchor.
2. **Candidate content** — typed columns describing what's being labeled
   (e.g., for `architect-gold`: `decision_id`, `verdict_emitted`,
   `task_id`, `pr_number`, `rationale_excerpt`, `gate_log_uri`).
3. **LLM recommendation** — `llm_label` (per-kind enum, typed), `llm_confidence`
   (`high | medium | low`), `llm_rationale` (Text), `llm_prompt_version` (Text),
   `llm_model` (Text). Closed CHECK constraints on the enum + confidence.
4. **Operator label** (nullable until reviewed) — `label_<verdict>` typed
   column per kind, plus `label_notes: Text | None`,
   `label_override_reason: Text | None` (filled only when the operator
   overrides the LLM recommendation).
5. **Labeled metadata** — `labeled_by: Text | None`, `labeled_at: TIMESTAMP | None`,
   `label_guidelines_version: Text | None` (which version of the rubric was
   live when this label was set; ensures retrospective re-labeling is
   detectable).
6. **Outcome** (optional, populated by downstream events) — `outcome_state`,
   `outcome_pr_merged_at`, etc. Mirrors the ADR-0061 outcome layer where
   applicable.

Schema responsibilities:
- Each kind owns its own Alembic migration.
- A shared Python `class ReviewQueueRowMixin` defines the six layers'
  contracts so new kinds inherit the discipline; `mypy` enforces the
  required columns.
- Indexes: partial index on `label_<verdict> IS NULL` for constant-time
  "next unlabeled" fetches (the ADR-0061 pattern).

### Auto-discovered routers

Each kind ships `services/api/treadmill_api/routers/dashboard/review_<kind>.py`
exporting a top-level `router = APIRouter(prefix="/api/v1/review/<kind>")`.
The dashboard router's existing `pkgutil.iter_modules` auto-discovery
(ADR-0056) mounts it on the next boot. Mandatory endpoints per kind:

- `GET /next?limit=N` — returns up to N unlabeled rows, ordered by
  `(llm_confidence ASC, created_at ASC)` so the operator sees the LLM's
  least-confident proposals first (highest-leverage labeling time).
- `GET /:id` — fetch one row in full (for deep-linking from notifications).
- `POST /:id/label` — write the operator label; row transitions to
  `labeled_at = now()`. Body: `{label: <verdict>, override_reason?: str,
  notes?: str}`.
- `GET /stats` — return per-kind health: `total`, `unlabeled`,
  `labeled_total`, `label_accuracy` (= fraction where operator's label
  matches LLM's recommendation), `accuracy_last_100`.

### Auto-discovered viewers

Each kind ships `services/dashboard/src/review/<kind>.tsx` exporting a
default React component. The dashboard's review module imports them via
`import.meta.glob` (Vite) and registers them in a kind→component map.
URL: `/review/<kind>` lands on a flip-through page rendering the queue with
that kind's viewer.

Contract for a viewer:
- Receives `{candidate, llm_recommendation, onLabel}` as props.
- Renders the candidate's content (per-kind layout).
- Renders the LLM's recommendation as a labeled card with confidence + rationale.
- Calls `onLabel({label, override_reason?, notes?})` when the operator acts.

Shared chrome (provided by the framework, not the viewer):
- Keyboard shortcuts: `space` = accept LLM recommendation, `x` = reject and
  expose the override reason field, `s` = skip, `?` = open guidelines for
  this kind, `j`/`k` = navigate.
- Confidence-bucket strip across the top showing how the operator's labels
  are distributing today.
- Per-kind label-accuracy widget pulled from `GET /stats`.

### The cybernetic loop wiring

For each kind:

1. The **proposing role** is itself a Treadmill role (sibling pattern to
   ADR-0061's `role-ui-triage`). It runs on a schedule (cadence per kind) or
   on-demand, scoring candidates and inserting rows with `llm_label`,
   `llm_confidence`, `llm_rationale`, `llm_prompt_version`, `llm_model`.
2. The **labeling UI** writes operator labels.
3. The **corpus exporter** runs on a schedule (per kind) and writes
   `<kind>-corpus.jsonl` artifacts to S3 + commits a snapshot to
   `docs/analysis/`. Format is whatever DSPy / the Wave 4 optimizer expects;
   reuse `judge_eval.py`'s `EvalResult` shape where applicable.
4. **Wave 4's retrospective scorer** (ADR-0056) treats each kind's
   label-accuracy fraction as the metric to optimize the proposing role's
   prompt against. DSPy proposes prompt variants; the **DSPy variant**
   queue itself becomes another review-queue kind (turtles all the way down,
   on purpose — the operator reviewing variant PRs is sanity-checking the
   meta-optimizer too).
5. As accuracy improves, the operator only sees rows where
   `llm_confidence < threshold` OR where retrospective accuracy on that
   slice is weak. Burden drops by design.

### What this generalizes

Eight kinds we already know we want, in priority order:

| Kind | Proposing role | Operator labels | First use |
|---|---|---|---|
| `architect-gold` | `role-architect-gold-proposer` | too-permissive / too-strict / correct / exclude | Wave 4's judge-prompt-optimizer corpus |
| `validator-gold` | `role-validator-gold-proposer` | correct-verdict / wrong-verdict / unclear | Same |
| `triage-finding` | existing `role-ui-triage` | is-real-bug / not-a-bug / dup | Promote existing ADR-0061 surface onto this framework |
| `crystallization-candidate` | existing crystallization role | promote / hold / drop | Learnings→rules pipeline |
| `dspy-variant-pr` | `role-dspy-variant-reviewer` | merge / revise / drop | Output side of Wave 4 |
| `auto-merge-eligible` | `role-pre-merge-judge` | merge / hold-for-review | Risk-aware auto-merge |
| `plan-pre-dispatch` | `role-plan-pre-judge` | dispatch / revise / drop | Auto-generated plans |
| `escalation-action` | `role-escalation-triage` | cancel / retry / fix-dispatch / leave | ADR-0062 incidents queue |

The triage-finding row's table doesn't move — it already conforms. We add
the framework around it, refactor the existing UI to use the shared chrome,
and keep one row schema.

### What this is NOT

- **Not a generic JSONB-keyed table.** Every kind is typed end-to-end.
- **Not a CMS.** No rich-text fields, no inline-editable rows. Labels are
  closed enums + a Text notes column.
- **Not a workflow engine.** The proposing role + labeling + corpus pipeline
  is composed from existing Treadmill primitives (roles, schedules, workflows,
  the dashboard's auto-discovery seams).
- **Not synchronous.** Operator labels are write-and-go; downstream
  consumers (corpus exporter, optimizer, retrospective scorer) read on their
  own cadence.

## Consequences

### Wins

- **One pattern for every review surface.** Adding a new kind is two files
  (router + viewer) plus a migration, plus a role to propose. Zero shared
  edits, by design (the ADR-0056 auto-discovery seam carries through).
- **Operator burden is measurable and shrinks over time.** Per-kind
  label-accuracy is a metric the optimizer minimizes deviations on. If a
  kind isn't shrinking, that's a signal — either the rubric is wrong, the
  guidelines are unclear, or the proposing prompt has a structural gap.
- **No new JSONB.** The architecture's "JSONB at three explicit sites" rule
  is preserved.
- **Existing surfaces converge.** ADR-0061's triage UI gets refactored onto
  the shared chrome without changing its table; the architect/validator gold
  scripts retire onto routed surfaces.

### Costs

- **N migrations.** Each kind brings a table + migration; the discipline is
  enforced by per-kind ownership.
- **Proposing-role authoring overhead.** Each new kind needs an LLM-as-judge
  role authored to produce the proposals. This is intentional — the cost is
  the lever; if a kind doesn't have a coherent judge prompt yet, it doesn't
  belong as a review queue.
- **Keyboard-shortcut chrome is shared.** A kind that wants different
  shortcuts has to extend the framework, not work around it. We accept this
  as the price of uniformity.

### Sequence

This ADR is design-only. Implementation follows after the judge-reliability
fixes (`docs/plans/2026-06-04-llm-judge-reliability-fixes.md`) land —
those fix the upstream input-starvation that makes any judge recommendation
trustworthy in the first place. Implementation plan would sequence:

1. **Framework substrate** — `ReviewQueueRowMixin`, shared dashboard chrome,
   keyboard handling, accuracy-stats endpoint, per-kind viewer registry.
2. **Refactor ADR-0061 triage onto the framework** — proves the abstraction
   on an already-working surface; no new schema needed.
3. **Ship `architect-gold` and `validator-gold` kinds** — the immediate
   ADR-0052 unblocker; corpus-exporter writes to S3 in the shape DSPy
   consumes.
4. **Ship `dspy-variant-pr`** — closes the Wave 4 review loop once those
   PRs start landing.
5. **Future kinds on demand** — `auto-merge-eligible`, `plan-pre-dispatch`,
   `crystallization-candidate`, `escalation-action` as their proposing
   judges mature.

## Open questions

- **Per-kind guidelines artifacts.** Each kind needs a versioned rubric
  document (`docs/labeling/<kind>.v1.md`) the proposing role + the operator
  share. Probably bundled into the role prompt the same way ADR-0061
  bundles its prompt artifact; pin in the implementation plan.
- **Cross-kind operator dashboards.** A single "label queue across all
  kinds, sorted by leverage" view would be valuable but is a v2 — the
  per-kind URL is the v1 contract.
- **Inter-kind dedupe.** Some candidates legitimately belong in two kinds
  (a triage finding that also seeds a crystallization rule). v1 lets the
  proposing roles double-emit; v2 might add cross-references. Don't
  over-design before we hit the case.
