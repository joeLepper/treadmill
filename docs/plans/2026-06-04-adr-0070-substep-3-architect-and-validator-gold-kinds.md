---
auto_merge: true
status: active
---

# Plan: ADR-0070 substep 3 — ship architect-gold + validator-gold review-queue kinds

- **Status:** active
- **Date:** 2026-06-04
- **Related ADRs:** ADR-0070 (the umbrella decision — pre-labeled review
  queues), ADR-0061 (TriageFindingRow precedent — the six-layer shape
  this generalises), ADR-0056 (dashboard auto-discovery), ADR-0053
  (Wave 4 / DSPy optimizer that consumes the exported corpora),
  ADR-0052 (judgment-role prompts from human-labeled corpora — the
  immediate unblocker)

## Goal

Stand up the first two non-triage review-queue kinds — `architect-gold`
and `validator-gold` — end-to-end against the substrate ADR-0070 §2
defines. Each kind ships its own typed Postgres table, auto-discovered
FastAPI router (`/next`, `/:id`, `/:id/label`, `/stats`), auto-discovered
React viewer at `/review/<kind>`, an LLM-as-judge "proposer" role that
inserts pre-labeled rows on demand, and a corpus exporter that writes
JSONL artifacts in the shape Wave 4's `evaluate_judge_prompt` /
`EvalResult` already consume. No new JSONB columns — the precedent table
shape from `TriageFindingRow` is the contract.

This substep assumes the framework substrate from ADR-0070 §sequence
step 1 (the `ReviewQueueRowMixin`, shared dashboard chrome, the
keyboard-shortcut + accuracy-stats wiring, the viewer registry) is
already in place — it lands in a sibling plan. This plan only adds two
concrete kinds on top of that substrate.

## Success criteria

1. Two new tables exist with passing Alembic migrations and matching
   SQLAlchemy ORM models: `architect_gold_rows` and
   `validator_gold_rows`. Each table carries the six ADR-0070 layers
   (provenance, candidate content, LLM recommendation, operator label,
   labeled metadata, outcome) using typed columns + CHECK constraints
   on every closed enum, with the partial unlabeled-index that keeps
   "next unlabeled" constant-time.
2. Two router modules auto-mount under the **review aggregator**
   (`/api/v1/review`) — the new `routers/review/` package substep 1
   ships, NOT the dashboard aggregator — via the same
   `pkgutil.iter_modules` discovery seam, at
   `services/api/treadmill_api/routers/review/review_architect_gold.py`
   and `.../review_validator_gold.py`, each exposing the four mandatory
   endpoints (`GET /next`, `GET /:id`, `POST /:id/label`, `GET /stats`)
   with closed Pydantic enums on the operator label body. Resulting
   public paths: `/api/v1/review/architect-gold/next` etc. Dashboard
   `src/api/` clients point at the `/api/v1/review/<kind>` paths via
   the framework's shared `useReviewQueue(kind)` hook (substep 1).
   Per-kind routers may hand-roll the four endpoints directly when
   the cross-field validation rules (e.g. `override_reason` required
   on disagreement) don't fit `build_review_router()`'s generic
   shape; living under `routers/review/` is what the auto-discovery
   aggregator keys on, NOT use of the factory.
3. Two React viewers mount under `/review/<kind>` via the framework's
   `import.meta.glob` registry at
   `services/dashboard/src/review/architect-gold.tsx` and
   `.../validator-gold.tsx`. Each renders the candidate panel + the
   LLM-recommendation card + calls the framework `onLabel` callback;
   no per-viewer keyboard handling (chrome owns it).
4. Two proposing LLM-as-judge roles exist in `services/api/treadmill_api/starters.py`
   — `role-architect-gold-proposer` and `role-validator-gold-proposer`
   — each with a multi-paragraph system prompt that describes the
   candidate intake shape, the closed label vocabulary, the JSON
   envelope it must emit (mapping 1:1 onto the row's `llm_*` columns),
   and the `OutputKind.ANALYSIS` output kind that lets Treadmill route
   the role's JSON into the row-insert path.
5. Two corpus exporters write JSONL artifacts that
   `evaluate_judge_prompt` can read directly: each row maps to one
   FLAT JSONL line — top-level `gold_verdict` (the operator's label,
   never the LLM's recommendation) plus the candidate input keys
   (e.g. `decision_id`, `verdict_emitted`, `rationale_excerpt`,
   `gate_log_uri`) at the top level. NO `input: {...}` wrapper — see
   `workers/agent/treadmill_agent/judge_eval.py::_compose_example_prompt`,
   which iterates top-level keys and renders each non-`gold_verdict`
   key as `## <Section>`. The exporter functions live under
   `services/api/treadmill_api/corpus_export.py`. The CLI seam is
   added to the existing Typer CLI at `cli/treadmill_cli/cli.py`
   (NOT `services/api/treadmill_api/cli.py`, which is a uvicorn
   launcher, not a Typer app). New subcommands: `treadmill corpus
   export architect-gold --out PATH` and `... validator-gold --out
   PATH`, each calling a new `POST /api/v1/corpus/<kind>/export`
   API endpoint that owns the DB read (preserves the ADR-0010
   contract that the CLI does HTTP, not direct DB access).
6. Every new public function/class has at least one happy-path and one
   error-path behavioural test against the real ORM models + the real
   route handlers (FastAPI `TestClient` against a stub session, mirroring
   `tests/test_routers_triage_labels.py`). Pydantic enums reject bad
   inputs at 422 in the route handler; CHECK constraints are documented
   in the migration and verified by the alembic upgrade smoke (live
   Postgres CHECK behavior is NOT asserted via the stub-session harness,
   since `_StubSession` does not invoke the DB engine).
7. AGENT.md updates per ADR-0030 on every touched component
   (`services/api/AGENT.md`, `services/dashboard/AGENT.md`) reference
   ADR-0070.

## Constraints / scope

### In scope

- Two Alembic migrations, two ORM rows, two routers, two viewers, two
  proposer roles, two corpus exporters.
- The CHECK constraints + closed Pydantic enums for the per-kind label
  vocabularies (architect-gold: `too-permissive | too-strict | correct
  | exclude`; validator-gold: `correct-verdict | wrong-verdict |
  unclear`).
- The corpus-exporter CLI surface — invocable on a schedule but the
  schedule itself is operator-owned and follows in a sibling plan.
- AGENT.md updates on the two services we touch.

### Out of scope

- The framework substrate (`ReviewQueueRowMixin`, shared dashboard
  chrome, keyboard handling, `import.meta.glob` viewer registry). That
  lands in ADR-0070 sequence step 1 in a separate plan; this plan
  assumes it is already in place.
- The ADR-0061 triage-finding refactor onto the substrate (sequence
  step 2). Triage is a separate plan.
- `dspy-variant-pr` (sequence step 4), `auto-merge-eligible`,
  `plan-pre-dispatch`, `crystallization-candidate`, `escalation-action`
  (sequence step 5). Each future kind gets its own plan riding the
  same template.
- Scheduling the proposer roles or the exporters. The proposer roles
  are runnable as on-demand workflows; schedules + cadence follow in
  ADR-0070 sequence step 4.
- Cross-kind operator dashboards (ADR-0070 open question — explicitly
  v2).
- Hooking the exported corpora into Wave 4's optimizer entrypoint —
  the exporter writes the artifact in the right shape; ADR-0053 Wave 4
  already knows how to consume `evaluate_judge_prompt`-compatible
  JSONL.

### Budget

Four worker dispatches. The router/viewer/role/exporter slices for one
kind go in a single PR per kind to keep the worker context tight and
the tests cohesive; the two kinds are sequenced so the second can
reuse any shape lessons from the first. If task 1 (architect-gold
tables + router) wedges at the architect cap, investigate before
shipping the validator-gold mirror — the shape will need to be
diagnosed before duplicating it.

## Sequence of work

```yaml
sequence_of_work:
  - id: architect-gold-table-and-router
    title: "ADR-0070 substep 3 task 1 — architect-gold migration, ORM row, router"
    workflow: wf-author
    intent: |
      STUDY:
        - `docs/adrs/0070-pre-labeled-review-queues.md` §"Per-kind table
          shape" — the six-layer contract this row must satisfy. Read
          the "Auto-discovered routers" section for the mandatory
          endpoints + the `(llm_confidence ASC, created_at ASC)`
          ordering on `GET /next`.
        - `services/api/treadmill_api/models/triage_finding.py` —
          `TriageFindingRow` is the precedent. Mirror its layer
          comments, its `CheckConstraint` style, its `Index` shape,
          and the partial unlabeled-index pattern. The architect-gold
          row replaces the "target state / evidence / detector output /
          dispatcher output" layers with one "candidate content" layer
          per ADR-0070 §2; keep provenance + label + labeled-metadata
          layers identical in spirit.
        - `services/api/alembic/versions/20260528_1400_triage_findings.py`
          — the migration shape to copy. `down_revision` must chain
          from the current head: at branch start, run
          `ls services/api/alembic/versions/ | sort | tail -3` and
          pick the latest by filename timestamp. Do NOT assume a
          specific filename — another migration may have merged
          between plan authoring and task dispatch.
        - `services/api/treadmill_api/routers/review/__init__.py` —
          the review-package auto-discovery seam shipped by substep 1
          (mirrors `routers/dashboard/__init__.py`'s pattern but
          mounted at `/api/v1/review`). Adding
          `review_architect_gold.py` with a module-level `router`
          attribute mounts it automatically; do NOT edit `__init__.py`.
        - `services/api/treadmill_api/routers/review/base.py` —
          substep 1's `build_review_router()` factory + `StatsResponse`
          shape. This task hand-rolls the router (the cross-kind
          factory is not used here because there is no `override_reason`
          cross-field rule in this kind, but per-kind label vocabulary
          is closed via a sibling Pydantic input model — see Task 3
          of substep 4 for the override-required pattern when needed).
        - `services/api/treadmill_api/routers/dashboard/overview.py` —
          Pydantic response shape conventions + the AsyncSession +
          `text()` pattern (use SQLAlchemy `select()` against the ORM
          model for `/next` + `/:id` + `/stats` since this is a
          single-table query; `text()` is overkill here).
        - `services/api/treadmill_api/routers/triage/labels.py` — the
          label-write pattern. The `record_label` seam + the
          `await session.refresh(existing)` round-trip are the model.

      BUILD:
        1. Migration:
           `services/api/alembic/versions/<NEW_TS>_architect_gold_rows.py`
           creating `architect_gold_rows` with columns:
           - Provenance: `id` (UUID PK, server_default `gen_random_uuid()`),
             `created_at` (TIMESTAMP server_default `now()`),
             `source_run_id` (UUID, nullable), `source_event_id`
             (UUID, nullable), `source_task_id` (UUID, nullable),
             `source_pr_number` (Integer, nullable), `source_url`
             (Text, nullable).
           - Candidate content: `decision_id` (Text, NOT NULL),
             `verdict_emitted` (String(32), NOT NULL — the architect's
             original verdict: `accept-as-is | amend | gate-broken`),
             `rationale_excerpt` (Text, NOT NULL),
             `gate_log_uri` (Text, nullable).
           - LLM recommendation: `llm_label` (String(32), NOT NULL —
             one of `too-permissive | too-strict | correct | exclude`),
             `llm_confidence` (String(8), NOT NULL — `high | medium |
             low`), `llm_rationale` (Text, NOT NULL),
             `llm_prompt_version` (Text, NOT NULL), `llm_model` (Text,
             NOT NULL).
           - Operator label (nullable): `label_verdict` (String(32),
             nullable, same closed set as `llm_label`),
             `label_notes` (Text, nullable),
             `label_override_reason` (Text, nullable).
           - Labeled metadata: `labeled_by` (Text, nullable),
             `labeled_at` (TIMESTAMP, nullable),
             `label_guidelines_version` (Text, nullable).
           - Outcome (optional): `outcome_state` (String(16),
             nullable), `outcome_pr_merged_at` (TIMESTAMP, nullable).
           CHECK constraints on every closed enum
           (`verdict_emitted`, `llm_label`, `llm_confidence`,
           `label_verdict IS NULL OR label_verdict IN (...)`,
           `outcome_state IS NULL OR outcome_state IN (...)`).
           Plain index on `created_at` and `verdict_emitted`. Partial
           index `ix_architect_gold_rows_unlabeled` on
           `label_verdict` `WHERE label_verdict IS NULL`.
           `down_revision` chains from the current alembic head.

        2. ORM row:
           `services/api/treadmill_api/models/architect_gold.py`
           defining `class ArchitectGoldRow(Base)` mirroring the
           migration. Use `Mapped[...]` typing throughout. Module
           docstring cites ADR-0070 and the six layers.

        3. Router:
           `services/api/treadmill_api/routers/review/review_architect_gold.py`
           with module-level `router = APIRouter(prefix="/architect-gold", tags=["review"])`. (Substep 1's review aggregator owns the `/api/v1/review` prefix; per-kind routers contribute only the kind segment. Final paths land at `/api/v1/review/architect-gold/...`.)
           Endpoints:
           - `GET /next?limit=N` (N default 20, max 200): return up to
             N unlabeled rows ordered by
             `(llm_confidence ASC, created_at ASC)` — encode confidence
             as a CASE expression mapping `low=0, medium=1, high=2` so
             "lowest confidence first" surfaces first under ORDER BY
             ASC (ADR-0070: least-confident proposals are highest-
             leverage labeling time). Add an explicit test case:
             seed one `high` + one `medium` + one `low` row and assert
             the response order is `[low_id, medium_id, high_id]`.
           - `GET /{row_id}`: fetch one row by UUID. 404 when missing.
           - `POST /{row_id}/label`: accepts a Pydantic body
             `{label: Literal[...the four verdicts], override_reason?:
             str | None, notes?: str | None, labeled_by: str}`. Stamps
             `labeled_at = now()`. 404 when missing; 409 when already
             labeled.
           - `GET /stats`: returns `{total, unlabeled, labeled_total,
             label_accuracy, accuracy_last_100}`. `label_accuracy` =
             fraction where `label_verdict = llm_label` over all
             labeled rows; `accuracy_last_100` is the same over the
             most recent 100 labeled rows by `labeled_at DESC`.
           Use Pydantic response models with field names matching the
           dashboard's `src/api/types.ts` discipline (snake_case).

      Tests (NEW behavioural tests, not just import checks):
        - `services/api/tests/test_models_architect_gold.py`:
          construct a row, insert via a stub session, assert the ORM
          accepts both fully-populated and label-null rows.
        - `services/api/tests/test_routers_review_architect_gold.py`:
          mirror `test_routers_triage_labels.py` style — stub
          `AsyncSession` + `get_session` dependency override.
            * Happy path GET /next returns rows in the expected order
              (assert low-confidence ordering by seeding the stub).
            * Happy path POST /{id}/label round-trips; the row
              transitions to non-null `labeled_at`.
            * POST 404 when id absent.
            * POST 409 when row already labeled.
            * POST 422 when `label` is not in the closed set
              (Pydantic enforces this — assert the response shape).
            * Auto-discovery contract test mirroring
              `test_routers_dashboard_init.py`'s synthetic-sibling
              check (the precedent; substep 1 ships an analogous
              test for the review aggregator) — assert
              `review_architect_gold` appears in
              `review_pkg.MOUNTED_MODULES` after
              `treadmill_api.routers.review` is freshly reloaded.
            * GET /stats reports `label_accuracy = matched / labeled`
              correctly when the stub session is seeded with 3 matched
              + 2 mismatched labels.

      AGENT.md update on services/api: add a NEW dedicated section
      header `## Review queues (ADR-0070)` near the related-ADR
      surface, with a sub-bullet `- architect-gold: ...` describing
      this task's table + router. Task 2 will append a sibling
      sub-bullet under the same header (avoids duplicate
      ADR-0070 paragraphs).
    scope:
      files:
        - services/api/alembic/versions/
        - services/api/treadmill_api/models/architect_gold.py
        - services/api/treadmill_api/routers/review/review_architect_gold.py
        - services/api/tests/test_models_architect_gold.py
        - services/api/tests/test_routers_review_architect_gold.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/models/triage_finding.py
        - services/api/treadmill_api/routers/triage/
        - services/api/treadmill_api/starters.py
        - services/dashboard/
    validation:
      - kind: deterministic
        description: |
          The new model + router test files exist and pass against the
          stub-session harness.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/tests/test_models_architect_gold.py" ]
          [ -f "$ROOT/services/api/tests/test_routers_review_architect_gold.py" ]
          cd "$ROOT/services/api" && uv run pytest tests/test_models_architect_gold.py tests/test_routers_review_architect_gold.py -q
      - kind: deterministic
        description: |
          A new alembic migration creating architect_gold_rows exists
          somewhere under alembic/versions/ (filename-timestamp
          robust per SKILL.md format-robustness rule).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -lE "architect_gold_rows" "$ROOT/services/api/alembic/versions/"*.py >/dev/null
      - kind: deterministic
        description: |
          The ORM model defines ArchitectGoldRow with the partial
          unlabeled index (asserts the six-layer contract surfaced).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -lE "class ArchitectGoldRow" "$ROOT/services/api/treadmill_api/models/architect_gold.py"
          grep -E "ix_architect_gold_rows_unlabeled" "$ROOT/services/api/alembic/versions/"*.py
      - kind: deterministic
        description: |
          The router module is auto-discoverable (module-level router
          attribute present; no edits to either review or dashboard
          __init__.py in this PR's full diff against origin/main, not
          just HEAD~1).
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -E "^router\s*=\s*(APIRouter|build_review_router)" "$ROOT/services/api/treadmill_api/routers/review/review_architect_gold.py"
          cd "$ROOT" && BASE=$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD~1)
          cd "$ROOT" && ! git diff --name-only "$BASE" HEAD -- services/api/treadmill_api/routers/review/__init__.py | grep -q __init__.py
          cd "$ROOT" && ! git diff --name-only "$BASE" HEAD -- services/api/treadmill_api/routers/dashboard/__init__.py | grep -q __init__.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0070 AND this PR touched it (defeats
          the pre-existing-string false-positive — once task 1 lands,
          downstream tasks would otherwise see the string for free).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -lE "ADR-0070" "$ROOT/services/api/AGENT.md"
          cd "$ROOT" && BASE=$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD~1)
          cd "$ROOT" && git diff --name-only "$BASE" HEAD -- services/api/AGENT.md | grep -q AGENT.md

  - id: validator-gold-table-and-router
    title: "ADR-0070 substep 3 task 2 — validator-gold migration, ORM row, router"
    workflow: wf-author
    depends_on: [task.architect-gold-table-and-router.pr_merged]
    intent: |
      STUDY:
        - The merged architect-gold migration, ORM row, and router
          from task 1. This task is a sibling mirror: identical shape,
          different candidate-content + label vocabulary.
        - `docs/adrs/0070-pre-labeled-review-queues.md` row "Kind |
          Proposing role | Operator labels | First use" — the
          validator-gold operator labels are
          `correct-verdict | wrong-verdict | unclear` and the
          candidate content is a validator decision (a `wf-validate`
          run's verdict + the artifact it judged).
        - `services/api/treadmill_api/models/run.py` — the
          `workflow_run_steps` table is where validator verdicts
          actually live (`output` JSONB carries the pass/fail call +
          script + artifact references). `task_validations` is the
          plan-spec DECLARATION (kind + description), NOT the
          executed verdict outcome. Use `workflow_run_steps.id` as
          the candidate-source FK target.
        - `services/api/treadmill_api/routers/review/__init__.py`
          auto-discovery seam — same as task 1, drop the file with a
          module-level `router` attribute under `routers/review/`, no
          edits to `__init__.py`.

      BUILD:
        1. Migration:
           `services/api/alembic/versions/<NEW_TS>_validator_gold_rows.py`
           creating `validator_gold_rows` with columns mirroring the
           architect-gold layer-by-layer:
           - Provenance: identical to architect-gold.
           - Candidate content: `source_step_id` (UUID, NOT NULL —
             FK to `workflow_run_steps.id ON DELETE SET NULL` since
             validator verdicts live there; mirrors the
             `triage_findings.parent_finding_id` FK pattern),
             `verdict_emitted` (String(8), NOT NULL — the validator's
             call: `pass | fail`), `script_excerpt` (Text, NOT NULL —
             the validation script that was run), `artifact_excerpt`
             (Text, NOT NULL — the stdout/stderr it judged).
             NOTE: do NOT FK to `task_validations` — that table
             stores plan-doc-declared checks (kind + description),
             not the executed verdict.
           - LLM recommendation: `llm_label` (String(32), NOT NULL —
             closed set `correct-verdict | wrong-verdict | unclear`),
             `llm_confidence` / `llm_rationale` / `llm_prompt_version`
             / `llm_model` — identical to architect-gold.
           - Operator label / labeled metadata / outcome: identical
             column shape to architect-gold (with `label_verdict`
             constrained to the validator vocabulary).
           CHECK constraints on `verdict_emitted`, `llm_label`,
           `llm_confidence`, `label_verdict`, `outcome_state`.
           Partial index `ix_validator_gold_rows_unlabeled` on
           `label_verdict WHERE label_verdict IS NULL`.
           `down_revision` chains from the current alembic head at
           branch start: `ls services/api/alembic/versions/ | sort
           | tail -3` and pick the latest by filename timestamp.
           Do NOT hard-code task 1's filename — it is determined at
           dispatch time.

        2. ORM row:
           `services/api/treadmill_api/models/validator_gold.py`
           defining `class ValidatorGoldRow(Base)` mirroring the
           migration. Module docstring cites ADR-0070.

        3. Router:
           `services/api/treadmill_api/routers/review/review_validator_gold.py`
           with `router = APIRouter(prefix="/validator-gold",
           tags=["review"])` — substep 1's review aggregator owns the
           `/api/v1/review` prefix; this router contributes only the
           kind segment. Final paths: `/api/v1/review/validator-gold/...`.
           Endpoint surface IDENTICAL to
           architect-gold's (`GET /next`, `GET /{id}`,
           `POST /{id}/label`, `GET /stats`) but the closed-enum
           Pydantic types swap to the validator vocabulary. Reuse
           the same `(llm_confidence ASC, created_at ASC)` ordering
           CASE.

      Tests (NEW behavioural tests):
        - `services/api/tests/test_models_validator_gold.py`: insert +
          retrieve a row via stub session; assert all six layers
          round-trip.
        - `services/api/tests/test_routers_review_validator_gold.py`:
          mirror task 1's router test file with the validator
          vocabulary. Same six coverage cases: GET /next ordering,
          POST round-trip, POST 404, POST 409 (re-label),
          POST 422 (bad enum), auto-discovery contract, GET /stats
          accuracy math.

      AGENT.md update on services/api: append a sub-bullet
      `- validator-gold: ...` under the existing
      `## Review queues (ADR-0070)` section task 1 created. Do NOT
      add a second ADR-0070 paragraph elsewhere in the file.
    scope:
      files:
        - services/api/alembic/versions/
        - services/api/treadmill_api/models/validator_gold.py
        - services/api/treadmill_api/routers/review/review_validator_gold.py
        - services/api/tests/test_models_validator_gold.py
        - services/api/tests/test_routers_review_validator_gold.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/models/architect_gold.py
        - services/api/treadmill_api/routers/review/review_architect_gold.py
        - services/api/treadmill_api/starters.py
        - services/dashboard/
    validation:
      - kind: deterministic
        description: |
          The new model + router test files exist and pass against the
          stub-session harness.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/tests/test_models_validator_gold.py" ]
          [ -f "$ROOT/services/api/tests/test_routers_review_validator_gold.py" ]
          cd "$ROOT/services/api" && uv run pytest tests/test_models_validator_gold.py tests/test_routers_review_validator_gold.py -q
      - kind: deterministic
        description: |
          A new alembic migration creating validator_gold_rows exists
          somewhere under alembic/versions/.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -lE "validator_gold_rows" "$ROOT/services/api/alembic/versions/"*.py >/dev/null
      - kind: deterministic
        description: |
          ORM model + partial unlabeled index present.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -lE "class ValidatorGoldRow" "$ROOT/services/api/treadmill_api/models/validator_gold.py"
          grep -E "ix_validator_gold_rows_unlabeled" "$ROOT/services/api/alembic/versions/"*.py
      - kind: deterministic
        description: |
          Router is auto-discoverable; neither review nor dashboard
          aggregator __init__.py is touched across the full PR diff
          (origin/main..HEAD), not just HEAD~1.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -E "^router\s*=\s*(APIRouter|build_review_router)" "$ROOT/services/api/treadmill_api/routers/review/review_validator_gold.py"
          cd "$ROOT" && BASE=$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD~1)
          cd "$ROOT" && ! git diff --name-only "$BASE" HEAD -- services/api/treadmill_api/routers/review/__init__.py | grep -q __init__.py
          cd "$ROOT" && ! git diff --name-only "$BASE" HEAD -- services/api/treadmill_api/routers/dashboard/__init__.py | grep -q __init__.py
      - kind: deterministic
        description: |
          Existing task-1 tests still pass (no regression on the
          architect-gold surface).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          cd "$ROOT/services/api" && uv run pytest tests/test_models_architect_gold.py tests/test_routers_review_architect_gold.py -q
      - kind: deterministic
        description: |
          This PR touched services/api/AGENT.md (defeats the
          pre-existing-string false-positive from task 1's edit).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -lE "ADR-0070" "$ROOT/services/api/AGENT.md"
          cd "$ROOT" && BASE=$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD~1)
          cd "$ROOT" && git diff --name-only "$BASE" HEAD -- services/api/AGENT.md | grep -q AGENT.md

  - id: gold-proposer-roles-and-viewers
    title: "ADR-0070 substep 3 task 3 — proposer roles + dashboard viewers for both kinds"
    workflow: wf-author
    depends_on: [task.validator-gold-table-and-router.pr_merged]
    intent: |
      STUDY:
        - `services/api/treadmill_api/starters.py` lines ~1103-1186 —
          the `role-prompt-optimizer` + `role-ui-triage` entries are
          the precedent for an LLM-as-judge role with
          `OutputKind.ANALYSIS`. Copy the dict shape exactly:
          `{id, model, output_kind, system_prompt}`. Use
          `WORKER_MODEL` (cheap) for fast iteration unless the role
          will be rarely-dispatched — both proposers are on-demand
          today, so `"claude-sonnet-4-6"` is the right tier (matches
          `role-prompt-optimizer`).
        - `services/dashboard/src/pages/TriageLabeling.tsx` — the
          precedent flip-through page. The new viewers are simpler:
          they receive `{candidate, llm_recommendation, onLabel}` as
          props per ADR-0070's viewer contract, and they render
          inside the framework-provided chrome (the substrate plan
          ships `<ReviewQueuePage kind="..." />` that hosts them).
        - `docs/adrs/0070-pre-labeled-review-queues.md` §"Auto-
          discovered viewers" — viewers do NOT handle keyboard
          shortcuts (the chrome does). They render the candidate
          content + the LLM recommendation card and call `onLabel`
          when the operator acts.
        - `services/api/tests/test_starters.py` (if it exists; grep
          for it) — the precedent for asserting role-registry
          additions don't break the seed surface.

      BUILD:
        1. In `services/api/treadmill_api/starters.py`, append two
           new entries to the `_ROLES` list:
           - `role-architect-gold-proposer`:
             system prompt instructing the role to read an architect
             decision (decision_id + verdict_emitted +
             rationale_excerpt + gate_log_uri), score it against
             the architect's job-spec (accept-as-is when the gates
             pass and the diff matches the spec; amend when minor
             issues; gate-broken when the gates themselves are
             broken), and emit a JSON envelope with fields mapping
             1:1 onto `architect_gold_rows.llm_*`:
             `{llm_label, llm_confidence, llm_rationale,
             llm_prompt_version, llm_model}`. Closed `llm_label`
             vocabulary: `too-permissive | too-strict | correct |
             exclude`. Model: `"claude-sonnet-4-6"`. OutputKind:
             `OutputKind.ANALYSIS`.
           - `role-validator-gold-proposer`: same shape, validator
             vocabulary: `correct-verdict | wrong-verdict | unclear`.
             The role reads a validator decision (validation_id +
             verdict_emitted + script_excerpt + artifact_excerpt)
             and scores whether the validator's pass/fail call
             matches the artifact. Same JSON envelope.
           Both prompts must:
             * Reference ADR-0070 in the opening line.
             * Pin the closed label vocabulary in the role text
               (the closed enum on the row is the contract; the
               role must produce a value in the set).
             * Spell out the JSON envelope inline using the same
               style as `role-prompt-optimizer`.
             * State that on parse failure the orchestrator will
               reject + re-dispatch (matches existing role conventions).

        2. React viewers:
           - `services/dashboard/src/review/architect-gold.tsx`
             default-exporting a `function ArchitectGoldViewer({
             candidate, llmRecommendation, onLabel })` that renders:
               * Candidate panel: `decision_id`, `verdict_emitted`
                 (as a `<StateBadge>`), `rationale_excerpt`
                 (paragraph), `gate_log_uri` (mono-spaced URI).
               * LLM recommendation card: `llm_label` (badge),
                 `llm_confidence` (badge), `llm_rationale`
                 (paragraph), `llm_prompt_version` + `llm_model`
                 (mono footer).
               * Label buttons: one per verdict in the closed set,
                 each wired to `onLabel({label: <verdict>})`.
                 Override-reason + notes inputs sit beside the
                 buttons; their values pass through `onLabel`.
             Style by re-using primitives from `src/design/` (Button,
             StateBadge, PageLayout helpers). Do NOT introduce a new
             design primitive.
           - `services/dashboard/src/review/validator-gold.tsx`
             mirroring the architect-gold viewer with the validator
             vocabulary + the validator candidate fields
             (`validation_id`, `verdict_emitted`, `script_excerpt`,
             `artifact_excerpt`).

      Tests:
        - `services/api/tests/test_starters_gold_proposers.py`: NEW
          file. Assert both role ids resolve via the
          `_ROLES_BY_ID` lookup (grep for that helper in starters.py
          first); assert each role's `system_prompt` references its
          closed-enum vocabulary string-literally
          (`"too-permissive"` ... etc) and that the prompt mentions
          `ADR-0070`. Assert `output_kind == OutputKind.ANALYSIS`.
        - `services/dashboard/src/review/architect-gold.test.tsx`
          (NEW; co-located per dashboard convention — see
          `src/design/Lifecycle.test.tsx`, `src/api/queries.test.tsx`,
          `src/pages/TaskDetail.test.tsx`; there is NO `__tests__/`
          directory anywhere under `services/dashboard/src/`): import
          `ArchitectGoldViewer` directly from `./architect-gold`,
          render it with a fixture candidate + recommendation; assert
          all six visible fields are present in the DOM
          (`decision_id`, `verdict_emitted`, etc.); pass a vi.fn()
          `onLabel`; assert clicking the `"correct"` button fires
          `onLabel({label: "correct"})`; assert clicking with an
          override-reason text passes it through.
        - `services/dashboard/src/review/validator-gold.test.tsx`:
          parallel coverage for the validator viewer.

      AGENT.md updates on services/api + services/dashboard
      referencing ADR-0070 + the two proposer roles + the viewer
      registry contract.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters_gold_proposers.py
        - services/dashboard/src/review/architect-gold.tsx
        - services/dashboard/src/review/validator-gold.tsx
        - services/dashboard/src/review/architect-gold.test.tsx
        - services/dashboard/src/review/validator-gold.test.tsx
        - services/api/AGENT.md
        - services/dashboard/AGENT.md
      services_affected:
        - services/api
        - services/dashboard
      out_of_scope:
        - services/api/treadmill_api/models/
        - services/api/treadmill_api/routers/dashboard/
        - services/api/alembic/versions/
        - services/dashboard/src/pages/
        - services/dashboard/src/App.tsx
    validation:
      - kind: deterministic
        description: |
          The new starters test passes (asserts both proposer roles
          are registered with the expected output_kind + reference
          ADR-0070 + carry their closed-enum vocabularies).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/tests/test_starters_gold_proposers.py" ]
          cd "$ROOT/services/api" && uv run pytest tests/test_starters_gold_proposers.py -q
      - kind: deterministic
        description: |
          Both viewers exist and export a default component;
          framework consumers can import them. Both co-located test
          files exist (per dashboard convention) and grep-prove they
          assert onLabel + the candidate fields render. NOTE: the
          worker sandbox does not run `npm ci`/`npx vitest` here
          (cost + time-budget); the substrate plan + CI run the
          actual vitest suite — this gate is presence + content
          grep only.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/dashboard/src/review/architect-gold.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/validator-gold.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/architect-gold.test.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/validator-gold.test.tsx" ]
          grep -E "export default" "$ROOT/services/dashboard/src/review/architect-gold.tsx"
          grep -E "export default" "$ROOT/services/dashboard/src/review/validator-gold.tsx"
          # Prove the tests actually exercise onLabel (defeat grep-only
          # author-evidence files): each test must import the viewer
          # and call onLabel via a click event.
          grep -E "onLabel" "$ROOT/services/dashboard/src/review/architect-gold.test.tsx"
          grep -E "onLabel" "$ROOT/services/dashboard/src/review/validator-gold.test.tsx"
          grep -E "from\s+['\"]\\./architect-gold['\"]" "$ROOT/services/dashboard/src/review/architect-gold.test.tsx"
          grep -E "from\s+['\"]\\./validator-gold['\"]" "$ROOT/services/dashboard/src/review/validator-gold.test.tsx"
      - kind: deterministic
        description: |
          Both proposer roles appear in starters.py with the
          right id + closed-enum vocabulary.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -E "role-architect-gold-proposer" "$ROOT/services/api/treadmill_api/starters.py"
          grep -E "role-validator-gold-proposer" "$ROOT/services/api/treadmill_api/starters.py"
          grep -E "too-permissive" "$ROOT/services/api/treadmill_api/starters.py"
          grep -E "correct-verdict" "$ROOT/services/api/treadmill_api/starters.py"
      - kind: deterministic
        description: |
          Starters import path resolves the two new roles via
          `_ROLES_BY_ID` and each carries `output_kind == ANALYSIS`
          (catches malformed dict entries that the string-grep gate
          above would let through).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          cd "$ROOT/services/api" && uv run python -c "from treadmill_api.starters import _ROLES_BY_ID; assert 'role-architect-gold-proposer' in _ROLES_BY_ID; assert 'role-validator-gold-proposer' in _ROLES_BY_ID; r1 = _ROLES_BY_ID['role-architect-gold-proposer']; r2 = _ROLES_BY_ID['role-validator-gold-proposer']; assert r1['output_kind'].name == 'ANALYSIS', r1['output_kind']; assert r2['output_kind'].name == 'ANALYSIS', r2['output_kind']"
      - kind: deterministic
        description: |
          AGENT.md updates on both services reference ADR-0070, AND
          this PR actually touched at least one of them (defeats the
          pre-existing-string false-positive — ADR-0070 may already
          appear in services/api/AGENT.md from task 1).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -lE "ADR-0070" "$ROOT/services/api/AGENT.md"
          grep -lE "ADR-0070" "$ROOT/services/dashboard/AGENT.md"
          cd "$ROOT" && BASE=$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD~1)
          cd "$ROOT" && git diff --name-only "$BASE" HEAD -- services/api/AGENT.md services/dashboard/AGENT.md | grep -q AGENT.md

  - id: gold-corpus-exporters
    title: "ADR-0070 substep 3 task 4 — corpus exporters for both kinds"
    workflow: wf-author
    depends_on: [task.gold-proposer-roles-and-viewers.pr_merged]
    intent: |
      STUDY:
        - `workers/agent/treadmill_agent/judge_eval.py` — the
          `EvalResult` dataclass + `_compose_example_prompt`
          (lines 72-86). The function iterates the example's
          TOP-LEVEL keys, skips `gold_verdict`, and renders each
          remaining key as `## <Section>\n<value>` (the section
          header maps via `_KEY_TO_SECTION` or falls back to the
          raw key). The exporter's JSONL must be FLAT: top-level
          per-field keys plus top-level `gold_verdict`. Do NOT
          wrap fields in an `input: {...}` dict — that would
          render one big mashed-up `## input\n{...repr...}` section
          and silently mis-feed Wave 4.
        - `workers/agent/tests/test_judge_eval.py` — confirms the
          fixture shape is FLAT (e.g. `{"diff": "d1", "gold_verdict":
          "pass"}`), validating the contract.
        - `cli/treadmill_cli/cli.py` — the REAL Typer CLI
          (`app = typer.Typer(name="treadmill", ...)` with
          `plan_app`, `task_app`, `workflows_app`, `role_app`,
          `learnings_app`, etc. registered via `app.add_typer`).
          NOTE: `services/api/treadmill_api/cli.py` is NOT a Typer
          app — it is the `treadmill-api` uvicorn console-script
          entrypoint (`def run() -> uvicorn.run(...)`). DO NOT
          add Typer subcommands there.
        - `cli/treadmill_cli/commands/learnings.py` — precedent for
          adding a new sub-app (`learnings_app = typer.Typer(...)`)
          and registering it on the main app via `app.add_typer`.
        - Per ADR-0010 the CLI wraps HTTP calls to the API; it
          does NOT open DB sessions. The exporter functions live
          in `services/api/treadmill_api/corpus_export.py` and the
          CLI calls a new `POST /api/v1/corpus/<kind>/export`
          endpoint that owns the DB read.
        - `services/api/treadmill_api/database.py` — the real
          session helpers are `make_session_factory(engine)` plus
          the `get_session` FastAPI dependency. There is no
          `with_session` / `async_session_maker` helper — use the
          existing `get_session` dependency in the router; the
          exporter functions themselves accept an `AsyncSession`.
        - `tools/load-analysis-corpus.sh` — the operator-facing
          push path. The exporter writes to `docs/analysis/<kind>-corpus.jsonl`
          locally; the operator pushes via the existing tool. (S3
          calls are NOT in the worker sandbox; the exporter writes
          local files only.)
        - `services/api/treadmill_api/models/architect_gold.py` and
          `.../validator_gold.py` (landed in tasks 1+2) — the row
          shapes the exporter reads.

      BUILD:
        1. Add a new module
           `services/api/treadmill_api/corpus_export.py` exporting:
             * `async def export_architect_gold(session: AsyncSession,
               out_path: Path) -> int` — selects every labeled row
               (label_verdict IS NOT NULL), emits one JSONL line per
               row to `out_path` with FLAT shape:
               `{"example_id": "<row_id>",
                  "decision_id": "...",
                  "verdict_emitted": "...",
                  "rationale_excerpt": "...",
                  "gate_log_uri": "...",
                  "gold_verdict": "<label_verdict>"}`.
               (`example_id` is metadata only — `_compose_example_prompt`
               will render it as `## example_id\n<uuid>`; the judge
               can ignore that section. NO `input` wrapper.)
               Returns the count of rows written.
             * `async def export_validator_gold(session: AsyncSession,
               out_path: Path) -> int` — mirror for validator-gold
               with FLAT keys `validation_id` (the
               `workflow_run_steps.id`), `verdict_emitted`,
               `script_excerpt`, `artifact_excerpt`, plus
               `gold_verdict`.
           Both functions accept an `AsyncSession` and an output
           `Path` so they're driven from tests + the API endpoint.

        2. Add a new API endpoint at
           `services/api/treadmill_api/routers/dashboard/corpus_export.py`
           with module-level `router = APIRouter(prefix="/corpus", tags=["corpus"])`
           (auto-discovered under `/api/v1/dashboard`) exposing:
             * `POST /architect-gold/export` body `{out_path: str}` →
               calls `export_architect_gold(session, Path(out_path))`,
               returns `{rows_written: int}`. Uses the `get_session`
               FastAPI dependency for the AsyncSession.
             * `POST /validator-gold/export` mirror.

        3. In `cli/treadmill_cli/cli.py`, register a new
           `corpus_app = typer.Typer(name="corpus", help="Corpus
           export operations.", no_args_is_help=True)` with two
           commands matching the precedent established by
           `learnings_app` / `schedules_app`:
             * `treadmill corpus export architect-gold --out PATH`
             * `treadmill corpus export validator-gold --out PATH`
           Each command POSTs to the corresponding API endpoint
           via `_client()` and prints `wrote N rows to <path>` on
           success (`N` is the response's `rows_written`). The CLI
           does NOT open a DB session — ADR-0010 keeps the CLI
           HTTP-only. Register the sub-app on the main `app` via
           `app.add_typer(corpus_app)`.

      Tests (NEW behavioural tests):
        - `services/api/tests/test_corpus_export.py`:
            * Happy path: seed an in-memory stub session with 3
              labeled architect-gold rows + 2 unlabeled; call
              `await export_architect_gold(session, tmp_path / "out.jsonl")`;
              assert the file contains exactly 3 lines; assert each
              line parses as JSON with FLAT top-level keys
              `decision_id`, `verdict_emitted`, `rationale_excerpt`,
              `gate_log_uri`, `gold_verdict` — and NO `input` key.
              Assert `gold_verdict` matches the row's `label_verdict`.
            * Same for validator-gold with its four FLAT input keys.
            * Edge case: zero labeled rows produces an empty file
              (not a crash) and the function returns 0.
            * Edge case: a row whose `gold_verdict` is somehow
              outside the closed set raises a ValueError BEFORE
              writing (the exporter is defensive — the row schema
              already enforces this, but the export path
              double-checks because the JSONL contract is
              `evaluate_judge_prompt`'s input).
            * JUDGE-EVAL COMPAT TEST (critical — pins the
              cross-service shape contract): import
              `_compose_example_prompt` from
              `treadmill_agent.judge_eval` (the workers/agent
              package is an editable workspace member; if the
              import fails, fall back to a path-hacked
              `importlib.util.spec_from_file_location` against
              `workers/agent/treadmill_agent/judge_eval.py`).
              Load one line of the emitted JSONL, call
              `_compose_example_prompt("PROMPT", line_dict)`, and
              assert the result contains both `## decision_id` and
              `## verdict_emitted` as separate section headers
              (proves the FLAT shape contract). The test fails
              loudly if anyone re-introduces an `input` wrapper.
        - `cli/tests/test_cli_corpus_export.py` (the existing CLI
          test convention — see `cli/tests/test_cli.py`,
          `test_cli_task_retry.py`, `test_cli_workflows_trigger.py`):
          invoke the new Typer commands via Typer's `CliRunner`
          with the API client patched to return a fixed
          `rows_written` count. Assert exit code 0 and that the
          row count appears in stdout (behavioural assertion — do
          NOT pin the exact string `"wrote N rows to ..."`; assert
          `"rows" in result.stdout.lower()` and the row count
          number appears).

      AGENT.md update on services/api referencing the new corpus-
      exporter surface + how it connects ADR-0070 to ADR-0053 Wave 4.
    scope:
      files:
        - services/api/treadmill_api/corpus_export.py
        - services/api/treadmill_api/routers/dashboard/corpus_export.py
        - cli/treadmill_cli/cli.py
        - services/api/tests/test_corpus_export.py
        - cli/tests/test_cli_corpus_export.py
        - services/api/AGENT.md
      services_affected:
        - services/api
        - cli
      out_of_scope:
        - services/api/treadmill_api/models/
        - services/api/treadmill_api/routers/triage/
        - services/api/treadmill_api/starters.py
        - services/api/alembic/versions/
        - services/api/treadmill_api/cli.py
        - services/dashboard/
        - workers/agent/
        - tools/load-analysis-corpus.sh
    validation:
      - kind: deterministic
        description: |
          The new server-side corpus-export tests exist and pass.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/tests/test_corpus_export.py" ]
          cd "$ROOT/services/api" && uv run pytest tests/test_corpus_export.py -q
      - kind: deterministic
        description: |
          The new CLI corpus-export tests exist and pass against the
          real treadmill Typer CLI (cli/treadmill_cli).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/cli/tests/test_cli_corpus_export.py" ]
          cd "$ROOT/cli" && uv run pytest tests/test_cli_corpus_export.py -q
      - kind: deterministic
        description: |
          The new corpus_export module exposes both exporter
          functions.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -E "def export_architect_gold" "$ROOT/services/api/treadmill_api/corpus_export.py"
          grep -E "def export_validator_gold" "$ROOT/services/api/treadmill_api/corpus_export.py"
      - kind: deterministic
        description: |
          The corpus-export API router module is auto-discoverable
          (real router seam landed under the dashboard aggregator).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -E "^router\s*=\s*APIRouter" "$ROOT/services/api/treadmill_api/routers/dashboard/corpus_export.py"
      - kind: deterministic
        description: |
          The real treadmill Typer CLI registers a corpus sub-app
          covering both kinds. Greps for both the Typer seam
          (`corpus_app = typer.Typer(`) and both subcommand slugs.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -E "corpus_app\s*=\s*typer\.Typer\(" "$ROOT/cli/treadmill_cli/cli.py"
          grep -E "architect-gold" "$ROOT/cli/treadmill_cli/cli.py"
          grep -E "validator-gold" "$ROOT/cli/treadmill_cli/cli.py"
      - kind: deterministic
        description: |
          No regression on prior-task suites.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          cd "$ROOT/services/api" && uv run pytest tests/test_models_architect_gold.py tests/test_models_validator_gold.py tests/test_routers_review_architect_gold.py tests/test_routers_review_validator_gold.py tests/test_starters_gold_proposers.py -q
      - kind: deterministic
        description: |
          AGENT.md references ADR-0070 AND this PR touched it (defeats
          the pre-existing-string false-positive).
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -lE "ADR-0070" "$ROOT/services/api/AGENT.md"
          cd "$ROOT" && BASE=$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD~1)
          cd "$ROOT" && git diff --name-only "$BASE" HEAD -- services/api/AGENT.md | grep -q AGENT.md
```

## Diagram

Not applicable. ADR-0070 carries the canonical
`LLM → queue → flip-through UI → label sink → corpus → cybernetic loop`
diagram this plan implements two concrete kinds against.

## Risks / unknowns

- **Framework substrate slippage.** This plan assumes the
  `ReviewQueueRowMixin` + shared dashboard chrome + viewer registry
  from ADR-0070 sequence step 1 are already in place. If they aren't
  when task 3 runs, the viewer mounts can't wire through the chrome.
  Mitigation: the per-kind viewers ship as default-exported pure
  components that take props matching the ADR-0070 contract, so if
  the registry slips they still render via direct import in the
  interim — and the viewer tests import them directly (via
  `from './architect-gold'`) rather than relying on the registry,
  so onLabel coverage is independent of the substrate's existence.
  The accuracy stats widget on the chrome can stub off
  `GET /stats` until the chrome lands.
- **Viewer tests are presence-+-grep-gated only, not vitest-run.**
  The worker sandbox can install nodejs but does not run
  `npm ci`/`npx vitest run` inside the validation gate (cost +
  time-budget). Gate enforces (a) the test files exist, (b) they
  import the viewer they target, (c) they reference `onLabel`.
  The actual vitest suite runs in CI / the substrate plan. A
  syntactically-broken viewer test would still pass the gate but
  fail CI; this is an accepted trade-off for keeping the worker
  loop fast.
- **Closed-enum drift between the migration, the ORM CHECK constraint,
  the Pydantic body, and the role prompt.** Four sources of truth on
  one vocabulary. Mitigation: the router test asserts a 422 on a
  bad-enum POST (proves Pydantic + the route are aligned), the
  starters test asserts each prompt string-literally contains the
  closed vocabulary tokens (proves the role can't drift silently),
  and the migration's CHECK constraint catches anything that slips
  past the application layer.
- **Validator-gold candidate FK.** The candidate is the executed
  validator's verdict, which lives in `workflow_run_steps`, NOT in
  `task_validations` (the latter stores plan-doc validation
  declarations). The plan FKs `source_step_id` to
  `workflow_run_steps.id ON DELETE SET NULL`. The risk is the
  worker copies the architect-gold provenance shape literally and
  picks the wrong FK target. Mitigation: task 2's `STUDY` step pins
  `workflow_run_steps` as the source-of-truth and explicitly
  forbids the `task_validations` FK; the model test's
  insert/round-trip exercises the FK constraint.
- **Corpus exporter shape drift from `evaluate_judge_prompt`.** If
  `EvalResult.per_example` changes shape upstream, the exporter's
  JSONL will silently mis-feed Wave 4. Mitigation: task 4's tests
  parse a known fixture line through the same `_parse_verdict` /
  example-shape contract `judge_eval.py` uses; a future change to
  that contract breaks the test before any corpus is written.
- **Auto-discovery surprise on a half-merged sibling.** If task 1's
  router lands but task 3's starters PR fails halfway, the route
  exists but the proposer role does not — the queue is empty.
  Mitigation: empty queues are an explicitly valid state (per ADR-0061
  triage's empty-state UI); the consequences are bounded. The
  `depends_on` chain serialises the four tasks so a halfway state
  doesn't compound.

## Decisions captured during execution

(empty at draft time)

## Post-mortem

(filled when plan transitions to completed)
