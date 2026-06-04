---
auto_merge: true
status: active
---

# Plan: ADR-0070 substep 4 — ship the `dspy-variant-pr` review-queue kind

- **Status:** active
- **Date:** 2026-06-04
- **Related ADRs:**
  - ADR-0070 (pre-labeled review queues — the framework + the
    eight-kind table this plan ships the `dspy-variant-pr` row of)
  - ADR-0061 (role-ui-triage — the precedent six-layer table + viewer
    pattern this plan mirrors; `TriageFindingRow` is the schema shape
    we mechanically copy)
  - ADR-0056 (operator dashboard auto-discovery — the
    `pkgutil.iter_modules` seam under
    `services/api/treadmill_api/routers/review/__init__.py` this
    plan drops a router into; substep 1 ships the review aggregator
    as a sibling of the dashboard aggregator using the same pattern)
  - ADR-0053 (judge-prompt optimization via workers — the Wave 4
    optimizer that emits the prompt-variant PRs this kind reviews)
  - ADR-0052 (judgment-role prompts from human-labeled corpora — the
    downstream consumer of the labeled corpus this kind feeds)

## Goal

Close the Wave 4 review loop by shipping a single pre-labeled
review-queue kind, `dspy-variant-pr`, end-to-end: a per-kind Postgres
table, an auto-discovered FastAPI router under
`routers/review/review_dspy_variant_pr.py`, a React viewer at
`/review/dspy-variant-pr`, and a proposing LLM-as-judge role
(`role-dspy-variant-reviewer`) registered in
`services/api/treadmill_api/starters.py`. The result is the operator-
sanity-check surface for Wave 4 optimizer-emitted prompt-variant PRs
— the meta-output of ADR-0053 that has had no operator-review home
since Wave 2 landed.

## Success criteria

1. A new Postgres table `review_dspy_variant_pr` exists with the six
   ADR-0070 layers (provenance, candidate content, LLM recommendation,
   operator label, labeled metadata, outcome), CHECK constraints on
   every closed enum, and a partial index on
   `label_verdict IS NULL` keeping "next unlabeled" constant-time.
2. A FastAPI router at
   `services/api/treadmill_api/routers/review/review_dspy_variant_pr.py`
   exposes the four ADR-0070-mandatory endpoints (`GET /next`,
   `GET /:id`, `POST /:id/label`, `GET /stats`) and is auto-mounted by
   the `pkgutil.iter_modules` discovery in
   `routers/review/__init__.py` (substep 1's review aggregator —
   sibling of the dashboard aggregator), without editing that file.
   Final mounted paths: `/api/v1/review/dspy-variant-pr/...`. The
   router hand-rolls the four handlers (does NOT use substep 1's
   `build_review_router` factory) because the
   `override_reason`-required-on-disagreement cross-field rule is
   kind-specific.
3. A React viewer component at
   `services/dashboard/src/review/dspy_variant_pr.tsx` renders one row
   at a time with the candidate panel (judge role, prompt path, both
   scores, the unified-diff patch) and the LLM recommendation card
   (`merge | revise | drop`, confidence, rationale) and submits the
   four-key label shape by calling
   `useLabelDspyVariantPr().mutate({id, label})` directly. (The
   `onLabel`-prop view-model from ADR-0070's "viewer contract" lands
   when substep 1 introduces `ReviewQueueRowMixin` + the shared
   viewer scaffold; this plan ships the literal-copy form.)
   `services/dashboard/src/App.tsx` registers `/review/dspy-variant-pr`
   as a route that mounts this viewer.
4. A new role `role-dspy-variant-reviewer` is added to the `_ROLES`
   list in `services/api/treadmill_api/starters.py`, follows the same
   shape as `role-prompt-optimizer` (Sonnet, `OutputKind.ANALYSIS`,
   multi-paragraph `system_prompt`), and its prompt is bundled at
   `services/api/treadmill_api/prompts/role_dspy_variant_reviewer_v1.md`
   loaded via the existing `_load_prompt(...)` helper.
5. Behavioral tests against real SQLAlchemy + the in-process API client
   cover happy + error paths for every endpoint, plus a `test_starters`
   invariant for the new role. The dashboard viewer gets a vitest
   smoke test asserting render + label-submit dispatch.
6. AGENT.md updates on every touched package per ADR-0030 reference
   ADR-0070 and the new `dspy-variant-pr` surface.

## Constraints / scope

### In scope

- The `review_dspy_variant_pr` Alembic migration (`upgrade()` +
  `downgrade()`), with the six-layer column set + indexes + CHECK
  constraints.
- ORM model at
  `services/api/treadmill_api/models/review_dspy_variant_pr.py`.
- Pydantic v2 schema at
  `services/api/treadmill_api/schemas/review_dspy_variant_pr.py`
  (request + response shapes, including the label POST body).
- Router file at
  `services/api/treadmill_api/routers/review/review_dspy_variant_pr.py`
  exporting a module-level `router = APIRouter(...)` so the existing
  dashboard auto-discovery mounts it.
- React viewer at
  `services/dashboard/src/review/dspy_variant_pr.tsx` + route
  registration in `services/dashboard/src/App.tsx` +
  query/mutation hooks in `services/dashboard/src/api/queries.ts` +
  TS types in `services/dashboard/src/api/types.ts`.
- Role registration in `services/api/treadmill_api/starters.py` (a
  single new entry in the `_ROLES` list) + the prompt artifact bundled
  under `services/api/treadmill_api/prompts/`.
- Behavioral tests: `services/api/tests/test_routers_review_dspy_variant_pr.py`,
  `services/api/tests/test_models_review_dspy_variant_pr.py`,
  vitest smoke at
  `services/dashboard/src/review/dspy_variant_pr.test.tsx`, and a
  case appended to `services/api/tests/test_starters.py` for the new
  role.
- AGENT.md edits on `services/api/AGENT.md` and
  `services/dashboard/AGENT.md` per ADR-0030.

### Out of scope

- The other seven ADR-0070 kinds (`architect-gold`, `validator-gold`,
  `triage-finding` refactor, `crystallization-candidate`,
  `auto-merge-eligible`, `plan-pre-dispatch`, `escalation-action`).
  This plan ships exactly one kind.
- Introducing the `ReviewQueueRowMixin` shared abstraction —
  that ships with substep 1. This plan's ORM inherits from
  `ReviewQueueRowMixin + Base` per substep 1's contract instead of
  duplicating the six-layer surface inline. If substep 1 hasn't
  landed at dispatch time, task 1's STUDY block must surface this
  and STOP — the row class cannot be authored against an
  unstable mixin contract.
- Authoring shared dashboard chrome (keyboard shortcuts strip,
  accuracy widget). Substep 1 ships these as
  `<ReviewPageLayout>` + `<KeyboardChrome>` + the accuracy widget.
  The viewer in this plan plugs into the substep-1 chrome via the
  per-kind viewer registry — it does NOT copy
  `TriageLabeling`-style page chrome.
- The corpus-exporter cron + the schedule that fires
  `role-dspy-variant-reviewer`. Both belong to ADR-0070 substep 4's
  *operations* follow-up, dispatched after this code lands and the
  table accumulates rows; this plan ships only the persistence +
  routing + viewer + role-registration surface.
- Promoting the existing `triage_findings` table onto the
  `/review/triage-finding` URL. Substep 2 of ADR-0070 covers that
  refactor and is explicitly a separate plan.
- Changes to `services/api/treadmill_api/routers/review/__init__.py`
  or `services/api/treadmill_api/app.py`. Auto-discovery is the
  ADR-0056 contract; if either file ends up in a diff the worker has
  done something wrong.

### Budget

Four worker dispatches, one per task. If task 2 or 3 wedges on
architect-amend more than twice, cancel and investigate before
re-dispatching; per memory, recurring loops on a single task usually
indicate a sandbox-shape mismatch in the validation gate or an
out-of-scope file-creep, not a real impasse.

## Sequence of work

```yaml
sequence_of_work:
  - id: review-dspy-variant-pr-table
    title: "ADR-0070 substep 4.1 — review_dspy_variant_pr table + ORM + Pydantic"
    workflow: wf-author
    intent: |
      STUDY (read but do not modify outside scope):
        - `services/api/treadmill_api/models/triage_finding.py` —
          the canonical six-layer shape. Mechanically copy its
          structure (provenance / target / evidence / detector /
          dispatcher / outcome / labels) into the new model,
          renaming the per-kind columns per ADR-0070's
          `dspy-variant-pr` row in the kind table.
        - `services/api/alembic/versions/20260528_1400_triage_findings.py`
          — the migration shape. Mirror its CHECK-constraint and
          partial-index discipline.
        - `services/api/treadmill_api/schemas/triage_finding.py` —
          the Pydantic shape (`extra='forbid'`, `from_attributes`,
          Literal enums, model_validators for cross-field
          invariants). Copy the structure.
        - `services/api/alembic/versions/20260604_0100_repo_configs_claude_account_fallback.py`
          — the most recent migration on `main`; its `revision`
          string is the `down_revision` for this new migration.

      BUILD:

      (1) New migration
          `services/api/alembic/versions/20260604_1200_review_dspy_variant_pr.py`:
          - `revision = "20260604_1200"`, `down_revision = "20260604_0100"`.
          - Create table `review_dspy_variant_pr` with columns
            in six layers:
              Provenance:
                - `id` UUID PK, server_default `gen_random_uuid()`.
                - `created_at` TIMESTAMP, server_default `now()`.
                - `source_run_id` UUID NOT NULL (the Wave 4
                  optimizer run that emitted the PR).
                - `source_pr_number` INTEGER NOT NULL (the
                  GitHub PR number being reviewed).
                - `source_pr_url` Text NOT NULL.
              Candidate content (typed; what's being labeled):
                - `judge_role` Text NOT NULL (the role whose
                  prompt is being optimized; e.g. `role-architect`).
                - `judge_prompt_path` Text NOT NULL (path the
                  optimizer read from).
                - `current_score` Numeric(5,4) NOT NULL.
                - `variant_score` Numeric(5,4) NOT NULL.
                - `improvement` Numeric(6,4) NOT NULL.
                - `patch_diff` Text NOT NULL (unified-diff body
                  the variant PR proposes).
                - `corpus_s3_uri` Text NOT NULL.
              LLM recommendation:
                - `llm_label` String(8) NOT NULL with CHECK
                  `llm_label IN ('merge','revise','drop')`.
                - `llm_confidence` String(8) NOT NULL with CHECK
                  `llm_confidence IN ('high','medium','low')`.
                - `llm_rationale` Text NOT NULL.
                - `llm_prompt_version` Text NOT NULL.
                - `llm_model` Text NOT NULL.
              Operator label (nullable until reviewed):
                - `label_verdict` String(8) NULL with CHECK
                  `label_verdict IS NULL OR label_verdict IN
                  ('merge','revise','drop')`.
                - `label_notes` Text NULL.
                - `label_override_reason` Text NULL.
              Labeled metadata:
                - `labeled_by` Text NULL.
                - `labeled_at` TIMESTAMP NULL.
                - `label_guidelines_version` Text NULL.
              Outcome (server-projected later; nullable now):
                - `outcome_state` String(16) NULL with CHECK
                  `outcome_state IS NULL OR outcome_state IN
                  ('pending','merged','rejected','superseded',
                  'cancelled')`.
                - `outcome_merged_at` TIMESTAMP NULL.
          - Indexes:
              - `ix_review_dspy_variant_pr_source_pr_number` on
                `source_pr_number`.
              - `ix_review_dspy_variant_pr_judge_role` on
                `judge_role`.
              - Partial index `ix_review_dspy_variant_pr_unlabeled`
                on `label_verdict` with
                `postgresql_where = text("label_verdict IS NULL")`
                — this is the constant-time "next unlabeled" path
                mandated by the ADR.
          - `downgrade()` mirrors `triage_findings`'s downgrade —
            drop the indexes in reverse, then the table.

      (2) New ORM model
          `services/api/treadmill_api/models/review_dspy_variant_pr.py`:
          - `class ReviewDspyVariantPrRow(Base)` with
            `__tablename__ = "review_dspy_variant_pr"`.
          - Column shapes (SQLAlchemy 2.0 `Mapped` annotations)
            mirror `TriageFindingRow` field-for-field per layer.
            Use `Numeric` from `sqlalchemy` for the score columns
            (Python `Decimal` at the ORM seam; Pydantic coerces to
            `float` at the schema layer — see (3) below).
          - `__table_args__` carries the same CHECK constraints +
            indexes as the migration so the model + DB stay in sync
            (this is the discipline `TriageFindingRow` follows).
            CHECK constraint NAMES must be explicit and stable:
            `ck_review_dspy_variant_pr_llm_label`,
            `ck_review_dspy_variant_pr_llm_confidence`,
            `ck_review_dspy_variant_pr_label_verdict`,
            `ck_review_dspy_variant_pr_outcome_state`. The non-DB
            test asserts these names are present on
            `__table__.constraints`.

      (2b) Edit `services/api/treadmill_api/models/__init__.py`:
          - Add `from treadmill_api.models.review_dspy_variant_pr
            import ReviewDspyVariantPrRow`.
          - Add `"ReviewDspyVariantPrRow",` to `__all__`
            (alphabetical position — between `RepoProfileRow` and
            `Role`, since `R-e-v` sorts after `R-e-p`).
          - REQUIRED: `alembic/env.py:25` imports
            `treadmill_api.models` to populate `Base.metadata`;
            without this edit, alembic autogenerate cannot see the
            new table, and `from treadmill_api.models import
            ReviewDspyVariantPrRow` (used by the tests + the router
            in task 2) fails.

      (3) New Pydantic schema
          `services/api/treadmill_api/schemas/review_dspy_variant_pr.py`:
          - `LlmLabelT = Literal["merge","revise","drop"]`.
          - `ConfidenceT = Literal["high","medium","low"]`.
          - `OutcomeStateT = Literal["pending","merged","rejected",
             "superseded","cancelled"]`.
          - `class ReviewDspyVariantPr(BaseModel)` with
            `ConfigDict(extra="forbid", from_attributes=True)` and
            all six layers' fields. **Scores are `float`** in the
            Pydantic schema (matches the TS `number` type and
            avoids Pydantic v2's default Decimal→string JSON
            serialization). The ORM column stays `Numeric(5,4)`
            so DB precision is preserved on write; SQLAlchemy
            coerces Numeric → float at the ORM seam on read.
            Pinned in the Risks section.
          - `class LabelDspyVariantPrRequest(BaseModel)` for the
            label POST body: `label_verdict: LlmLabelT`,
            `label_notes: str | None = None`,
            `label_override_reason: str | None = None`,
            `labeled_by: str = Field(..., min_length=1)`. Add a
            `model_validator(mode="after")` enforcing:
            `label_override_reason` MUST be non-null when
            `label_verdict` differs from the row's `llm_label` —
            i.e., the request itself can't enforce this (it
            doesn't carry `llm_label`); the router does that
            cross-check. The schema-level validator instead
            enforces a positive form: when `label_override_reason`
            is supplied it must be non-empty. Mirror the
            cross-field validator pattern from
            `triage_finding.py::_check_suppression_signal`.

      (4) Tests — TWO SEPARATE FILES, sandbox-safe vs integration.

          The worker sandbox has NO Postgres and does NOT set
          `TREADMILL_INTEGRATION=1`. The precedent `test_triage_store.py`
          gates its round-trip cases behind `@integration =
          pytest.mark.skipif(not INTEGRATION, ...)` at lines 177-181;
          there is no shared `client`/`db_session` fixture and no
          conftest.py under `services/api/tests/`. Split this work
          so the sandbox gate actually runs SOMETHING.

          File A — `services/api/tests/test_models_review_dspy_variant_pr.py`
          (NON-DB; always runs in the sandbox):
          - `test_table_name` — assert
            `ReviewDspyVariantPrRow.__tablename__ == "review_dspy_variant_pr"`.
          - `test_check_constraint_names_present` — introspect
            `ReviewDspyVariantPrRow.__table__.constraints` and assert
            the three CHECK constraint names exist:
            `ck_review_dspy_variant_pr_llm_label`,
            `ck_review_dspy_variant_pr_llm_confidence`,
            `ck_review_dspy_variant_pr_label_verdict`,
            `ck_review_dspy_variant_pr_outcome_state`.
          - `test_partial_index_present` — walk
            `ReviewDspyVariantPrRow.__table__.indexes`, find
            `ix_review_dspy_variant_pr_unlabeled`, assert its
            dialect_kwargs include a `postgresql_where` whose
            compiled text contains `label_verdict IS NULL`.
          - `test_pydantic_schema_round_trip` — build a
            `ReviewDspyVariantPr` from a SimpleNamespace mirroring
            the ORM attrs (use `model_validate` with
            `from_attributes=True`), assert no extra keys, and
            assert `LlmLabelT` rejects an invalid value (`Pydantic
            ValidationError` on `llm_label="bogus"`).
          - `test_pydantic_score_json_round_trip` — instantiate
            the schema with `current_score=0.7543`, call
            `.model_dump_json()`, assert the substring `0.7543`
            (NOT `"0.7543"`) appears; round-trip via
            `model_validate_json` back to assert equality. Proves
            the float decision survives the JSON seam.
          - `test_label_request_requires_labeled_by` — instantiate
            `LabelDspyVariantPrRequest(label_verdict="merge")`
            without `labeled_by` and assert a `ValidationError`.
          - `test_label_request_override_reason_non_empty_when_supplied`
            — instantiate with `label_override_reason=""` and
            assert a `ValidationError` from the model_validator.

          File B — `services/api/tests/test_models_review_dspy_variant_pr_integration.py`
          (Postgres round-trip; marked `@integration`, mirrors
          `test_triage_store.py:177-216`):
          - `test_insert_round_trip_persists_six_layers`.
          - `test_llm_label_check_constraint_rejects_invalid` —
            assert `sqlalchemy.exc.IntegrityError` matching
            `ck_review_dspy_variant_pr_llm_label`.
          - `test_label_verdict_check_constraint_rejects_invalid`.
          - `test_unlabeled_partial_index_query_returns_only_nulls`.
          Each guarded by `@integration` so the sandbox SKIPs
          them cleanly; CI / `treadmill-local up` runs them with
          `TREADMILL_INTEGRATION=1`.

          (Build the integration file's async session fixtures by
          copying the `database_url` / `engine` / `truncate`
          fixtures from `test_triage_store.py:188-220` into the
          new file directly — there is no shared conftest to
          reuse.)

      DOC:
        - `services/api/AGENT.md` Recent-changes entry — one
          paragraph citing ADR-0070 substep 4 and the new
          `review_dspy_variant_pr` table + ORM + schema.

      OUT-OF-SCOPE (do not touch):
        - The router (`routers/review/review_dspy_variant_pr.py`)
          — task 2.
        - `triage_finding.py` (model or schema).
        - Any other table or model.
        - `app.py` or `routers/review/__init__.py`.
        - The role list in `starters.py` — task 4.
    scope:
      files:
        - services/api/alembic/versions/20260604_1200_review_dspy_variant_pr.py
        - services/api/treadmill_api/models/review_dspy_variant_pr.py
        - services/api/treadmill_api/models/__init__.py
        - services/api/treadmill_api/schemas/review_dspy_variant_pr.py
        - services/api/tests/test_models_review_dspy_variant_pr.py
        - services/api/tests/test_models_review_dspy_variant_pr_integration.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/routers/review/__init__.py
        - services/api/treadmill_api/app.py
        - services/api/treadmill_api/starters.py
        - services/api/treadmill_api/models/triage_finding.py
        - services/api/treadmill_api/schemas/triage_finding.py
        - services/dashboard/
    validation:
      - kind: deterministic
        description: |
          ORM + schema + migration files exist; models/__init__.py
          registers the new row; the NON-DB tests in
          test_models_review_dspy_variant_pr.py pass (and are
          actually collected, not all skipped — the sandbox has no
          Postgres so the integration file is gated on
          TREADMILL_INTEGRATION=1 and SKIPs cleanly).
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/treadmill_api/models/review_dspy_variant_pr.py" ]
          [ -f "$ROOT/services/api/treadmill_api/schemas/review_dspy_variant_pr.py" ]
          [ -f "$ROOT/services/api/tests/test_models_review_dspy_variant_pr.py" ]
          grep -q "ReviewDspyVariantPrRow" "$ROOT/services/api/treadmill_api/models/__init__.py"
          ls "$ROOT/services/api/alembic/versions/"*review_dspy_variant_pr*.py >/dev/null
          cd "$ROOT/services/api" && uv run pytest tests/test_models_review_dspy_variant_pr.py -v
          # Pin that the load-bearing non-DB tests actually ran (not skipped):
          cd "$ROOT/services/api" && uv run pytest tests/test_models_review_dspy_variant_pr.py::test_check_constraint_names_present tests/test_models_review_dspy_variant_pr.py::test_pydantic_score_json_round_trip tests/test_models_review_dspy_variant_pr.py::test_label_request_requires_labeled_by -v
      - kind: deterministic
        description: |
          The migration's down_revision points at the current head
          on main (20260604_0100); the table name + key columns
          match the ADR-0070 six-layer contract.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          MIG=$(ls "$ROOT/services/api/alembic/versions/"*review_dspy_variant_pr*.py | head -1)
          [ -n "$MIG" ]
          grep -q "down_revision" "$MIG"
          grep -q '20260604_0100' "$MIG"
          grep -q "review_dspy_variant_pr" "$MIG"
          grep -q "label_verdict" "$ROOT/services/api/treadmill_api/models/review_dspy_variant_pr.py"
          grep -q "llm_label" "$ROOT/services/api/treadmill_api/models/review_dspy_variant_pr.py"
          grep -q "judge_role" "$ROOT/services/api/treadmill_api/models/review_dspy_variant_pr.py"
      - kind: deterministic
        description: |
          AGENT.md references ADR-0070 and the new kind.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "ADR-0070" "$ROOT/services/api/AGENT.md"
          grep -q "review_dspy_variant_pr" "$ROOT/services/api/AGENT.md"

  - id: review-dspy-variant-pr-router
    title: "ADR-0070 substep 4.2 — auto-discovered router under routers/review/"
    workflow: wf-author
    depends_on: [task.review-dspy-variant-pr-table.pr_merged]
    intent: |
      STUDY:
        - `services/api/treadmill_api/routers/review/__init__.py`
          — the pkgutil auto-discovery seam shipped by substep 1.
          Every sibling `.py` under `routers/review/` exporting a
          module-level `router = APIRouter()` is mounted under
          `/api/v1/review`. Do NOT edit this file; just drop a
          sibling.
        - `services/api/treadmill_api/routers/review/base.py` —
          substep 1's `build_review_router()` factory. This task
          does NOT use the factory (kind-specific override_reason
          cross-field rule); the file is studied for the
          auto-discovery contract + the `StatsResponse` shape only.
        - `services/api/treadmill_api/routers/triage/labels.py` —
          the existing labeling router pattern (the GET unlabeled
          + POST label dance). Mirror its session/DI shape:
          `Annotated[AsyncSession, Depends(get_session)]`,
          `Pydantic` body model, 404 on missing row, refresh +
          return after commit.
        - `services/api/treadmill_api/routers/dashboard/overview.py`
          — shape reference for an auto-discovered router endpoint
          (the dashboard aggregator follows the same pattern).
        - The ADR-0070 endpoints to mount:
            GET  /review/dspy-variant-pr/next?limit=N
            GET  /review/dspy-variant-pr/{id}
            POST /review/dspy-variant-pr/{id}/label
            GET  /review/dspy-variant-pr/stats

          The review aggregator (substep 1's `routers/review/`
          package) mounts everything under `/api/v1/review`, so the
          router's own prefix is `/dspy-variant-pr` and the final
          paths land at `/api/v1/review/dspy-variant-pr/...`. Use
          `router = APIRouter(prefix="/dspy-variant-pr",
          tags=["review", "dspy-variant-pr"])`.

      BUILD:

      File: `services/api/treadmill_api/routers/review/review_dspy_variant_pr.py`.

      Implement four handlers:

      (1) `GET /next?limit=N`:
          - `limit: Annotated[int, Query(ge=1, le=200)] = 50`.
          - Order by `(llm_confidence ASC, created_at ASC)` per
            ADR-0070 — least-confident first. Implement the
            ordering with a CASE expression mapping
            `low->0, medium->1, high->2` (low is "least confident"
            per the LLM-recommendation column's semantics — the
            ADR explicitly wants the operator to see those first),
            so `ORDER BY confidence_rank ASC, created_at ASC`.
          - Filter `WHERE label_verdict IS NULL`.
          - Return `list[ReviewDspyVariantPr]`.

      (2) `GET /{id}`:
          - `id: uuid.UUID` path param.
          - 404 on missing; return the row as
            `ReviewDspyVariantPr`.

      (3) `POST /{id}/label`:
          - Body: `LabelDspyVariantPrRequest` from task 1's schema.
          - 404 on missing.
          - Cross-field check: if `body.label_verdict !=
            row.llm_label` and `body.label_override_reason is None`,
            raise `HTTPException(status_code=422,
            detail="override_reason required when overriding the
            LLM recommendation")`.
          - Persist `label_verdict`, `label_notes`,
            `label_override_reason`, `labeled_by`, set
            `labeled_at = func.now()` (use `sqlalchemy.func.now()`
            so the DB stamps it), and `label_guidelines_version`
            from a module-level constant `LABEL_GUIDELINES_VERSION
            = "v1"` (this lets future rubric bumps be detected).
          - `await session.commit()`, `await session.refresh(row)`,
            return as `ReviewDspyVariantPr`.

      (4) `GET /stats`:
          - Return:
              total: int
              unlabeled: int
              labeled_total: int
              label_accuracy: float | None  # null when labeled_total == 0
              accuracy_last_100: float | None  # null when labeled_total < 100
          - `label_accuracy` = fraction of labeled rows where
            `label_verdict == llm_label`.
          - `accuracy_last_100` = same fraction over the most recent
            100 labeled rows ordered by `labeled_at DESC`.
          - Implement with a single SELECT that counts each bucket;
            don't pull rows into Python.

      Module-level constant `LABEL_GUIDELINES_VERSION = "v1"` near
      the top of the file with a comment citing ADR-0070 §"Labeled
      metadata".

      The router MUST be a module-level `router = APIRouter(...)`
      so the existing `_discover_and_mount` finds it. No edits to
      `routers/review/__init__.py` or `app.py`.

      Tests file:
      `services/api/tests/test_routers_review_dspy_variant_pr.py`.

      Mirror the `_StubSession` + `_build_app(session)` + `TestClient(app)`
      pattern from `tests/test_routers_triage_labels.py:63-108` and
      `tests/test_routers_triage_findings.py:41-58`. There is NO
      shared `client` or `db_session` fixture and no conftest.py
      under `services/api/tests/` — each test builds its own
      FastAPI app via `app.dependency_overrides[get_session] =
      lambda: _override_session()` and instantiates `TestClient(app)`
      inline. Patch the store-seam (router-level helper that issues
      SQL) per-test the same way `labels_mod.TriageStore` is patched
      so `_StubSession.execute` stays a defensive no-op.

      Tests that require SQL-correctness assertions (ORDER BY
      confidence, partial-index WHERE label_verdict IS NULL, COUNT
      math for `/stats`) CANNOT round-trip through the stub —
      either:
        (a) split them into a sibling file
            `tests/test_routers_review_dspy_variant_pr_integration.py`
            marked `@integration` (mirrors the `@integration` gate
            in `test_triage_store.py:177-181`); OR
        (b) assert against a mock store whose
            `get_next` / `record_label` / `stats` calls are patched
            to return canned shapes — the test then asserts the
            HTTP-shape + that the store was called with the right
            kwargs (ordering / filtering becomes a store-layer
            integration concern, not a router-unit concern).

      Pick (b) for: test_get_next_returns_unlabeled_only,
      test_get_next_orders_low_confidence_first,
      test_get_next_honors_limit,
      test_stats_label_accuracy_math,
      test_stats_returns_zero_when_empty,
      test_post_label_persists_and_stamps_labeled_at — each asserts
      `mock_store.method.assert_called_with(...)` plus the HTTP
      response shape, NOT the SQL semantics. The store-layer
      integration test (sibling @integration file, gated like
      test_triage_store.py) covers real SQL.

      Cover (all in the unit file unless noted):

        - `test_get_next_returns_unlabeled_only` — insert two
          unlabeled + one labeled row, GET /next, expect length 2.
        - `test_get_next_orders_low_confidence_first` — insert
          three rows with confidence high/medium/low, expect the
          response order [low, medium, high].
        - `test_get_next_honors_limit` — insert 5 rows, GET
          /next?limit=2, expect length 2.
        - `test_get_by_id_404_when_missing` — random UUID, expect
          404.
        - `test_get_by_id_returns_full_row` — known id, assert all
          six layers' fields present in JSON.
        - `test_post_label_persists_and_stamps_labeled_at` —
          POST a label, assert row is updated, `labeled_at` is not
          null, `label_guidelines_version == "v1"`.
        - `test_post_label_404_when_missing` — random UUID, 404.
        - `test_post_label_requires_override_reason_when_disagreeing`
          — row has `llm_label="merge"`, POST label_verdict="drop"
          without `label_override_reason`, expect 422.
        - `test_post_label_accepts_agreement_without_reason` —
          POST label_verdict equal to llm_label without
          override_reason, expect 200.
        - `test_post_label_invalid_verdict_rejected_by_pydantic`
          — POST with `label_verdict="bogus"`, expect 422.
        - `test_stats_returns_zero_when_empty` — empty table,
          GET /stats, assert
          `{total: 0, unlabeled: 0, labeled_total: 0,
           label_accuracy: null, accuracy_last_100: null}`.
        - `test_stats_label_accuracy_math` — insert 4 labeled
          rows (3 agree with llm_label, 1 disagrees), assert
          `label_accuracy == 0.75`.
        - `test_router_auto_mounted_under_review` — read the
          FastAPI app's routes and assert the path
          `/api/v1/review/dspy-variant-pr/next` exists.
          Mirror the assertion from the auto-discovery contract
          test that substep 1 ships alongside its aggregator (the
          shape parallels `tests/test_routers_dashboard_init.py:41-47`'s
          `test_overview_router_is_auto_discovered`):
          ```
          from treadmill_api.routers import review as review_pkg
          assert "review_dspy_variant_pr" in review_pkg.MOUNTED_MODULES
          paths = {getattr(r, "path", None) for r in review_pkg.router.routes}
          assert "/api/v1/review/dspy-variant-pr/next" in paths
          ```

      DOC:
        - Extend the `services/api/AGENT.md` Recent-changes entry
          from task 1 to also cite the router endpoints. Note that
          the React viewer at `/review/dspy-variant-pr` ships in a
          follow-up PR (this plan's task 3); the router is
          independently exercisable via curl until then.

      OUT-OF-SCOPE:
        - The ORM model + schema (task 1, already merged).
        - The viewer / dashboard React side (task 3).
        - `routers/review/__init__.py` (auto-discovery handles
          mounting).
        - `app.py`.
        - The role / `starters.py` (task 4).
    scope:
      files:
        - services/api/treadmill_api/routers/review/review_dspy_variant_pr.py
        - services/api/tests/test_routers_review_dspy_variant_pr.py
        - services/api/tests/test_routers_review_dspy_variant_pr_integration.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/routers/review/__init__.py
        - services/api/treadmill_api/app.py
        - services/api/treadmill_api/starters.py
        - services/api/treadmill_api/models/review_dspy_variant_pr.py
        - services/api/treadmill_api/schemas/review_dspy_variant_pr.py
        - services/dashboard/
    validation:
      - kind: deterministic
        description: |
          Router file exists, exports a module-level router, and
          all unit-level behavioral tests pass. (Stub-session unit
          file runs in the sandbox; the @integration sibling file
          SKIPs cleanly without TREADMILL_INTEGRATION=1.) The
          auto-discovery negative-grep gate is sequenced AFTER
          pytest so the source of truth is the test, not the grep.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/treadmill_api/routers/review/review_dspy_variant_pr.py" ]
          [ -f "$ROOT/services/api/tests/test_routers_review_dspy_variant_pr.py" ]
          grep -q "router = APIRouter" "$ROOT/services/api/treadmill_api/routers/review/review_dspy_variant_pr.py"
          cd "$ROOT/services/api" && uv run pytest tests/test_routers_review_dspy_variant_pr.py -v
          # Auto-discovery: the new module is NOT imported by name
          # from app.py or the dashboard __init__.py (the
          # test_router_auto_mounted_under_dashboard test above is
          # the positive signal; this is the negative complement).
          (! grep -q "review_dspy_variant_pr" "$ROOT/services/api/treadmill_api/app.py")
          (! grep -q "review_dspy_variant_pr" "$ROOT/services/api/treadmill_api/routers/review/__init__.py")
      - kind: deterministic
        description: |
          AGENT.md continues to reference ADR-0070 + cites the
          router endpoints.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "ADR-0070" "$ROOT/services/api/AGENT.md"
          grep -q "review/dspy-variant-pr" "$ROOT/services/api/AGENT.md"

  - id: review-dspy-variant-pr-viewer
    title: "ADR-0070 substep 4.3 — React viewer + route + query hooks"
    workflow: wf-author
    depends_on: [task.review-dspy-variant-pr-router.pr_merged]
    intent: |
      STUDY:
        - `services/dashboard/src/pages/TriageLabeling.tsx` —
          the labeling-UI precedent. Mechanically mirror the
          two-column layout (evidence on the left, label form on
          the right), the `PageLayout` chrome, and the optimistic
          drop-on-submit behavior. Per ADR-0070 §"Auto-discovered
          viewers" the eventual contract is
          `services/dashboard/src/review/<kind>.tsx`; this plan
          places the new viewer there.
        - `services/dashboard/src/api/queries.ts` lines around
          130-182 — the `useUnlabeledFindings` / `useLabelFinding`
          hook shape, including the optimistic-update pattern via
          `onMutate` / `onError` / `onSettled`. Mirror it for the
          new kind.
        - `services/dashboard/src/api/types.ts` — the type-defs
          surface. Add the new shapes there.
        - `services/dashboard/src/App.tsx` — the routing table.
          Register the new route.
        - `services/dashboard/src/api/queries.test.tsx` — the
          vitest pattern (renderHook + wrapper + mocked fetch).
          Mirror for the smoke test.

      BUILD:

      (1) `services/dashboard/src/api/types.ts` — append:
            export type DspyVariantPrLabel = 'merge' | 'revise' | 'drop';
            export type DspyVariantPrConfidence = 'high' | 'medium' | 'low';

            export interface DspyVariantPrRow {
              id: string;
              created_at: string;
              source_run_id: string;
              source_pr_number: number;
              source_pr_url: string;
              judge_role: string;
              judge_prompt_path: string;
              current_score: number;
              variant_score: number;
              improvement: number;
              patch_diff: string;
              corpus_s3_uri: string;
              llm_label: DspyVariantPrLabel;
              llm_confidence: DspyVariantPrConfidence;
              llm_rationale: string;
              llm_prompt_version: string;
              llm_model: string;
              label_verdict: DspyVariantPrLabel | null;
              label_notes: string | null;
              label_override_reason: string | null;
              labeled_by: string | null;
              labeled_at: string | null;
              label_guidelines_version: string | null;
              outcome_state: string | null;
              outcome_merged_at: string | null;
            }

            export interface DspyVariantPrLabelInput {
              label_verdict: DspyVariantPrLabel;
              label_notes?: string | null;
              label_override_reason?: string | null;
              labeled_by: string;
            }

      (2) `services/dashboard/src/api/queries.ts` — append a new
          section after the triage section:

            /* ─── DSPy variant PR review (ADR-0070) ─────────────── */

            const DSPY_VARIANT_PR_KEY = ['review', 'dspy-variant-pr'] as const;

            export function useDspyVariantPrQueue(limit = 50) {
              return useQuery({
                queryKey: [...DSPY_VARIANT_PR_KEY, 'next', limit],
                queryFn: async () =>
                  _apiFetch<DspyVariantPrRow[]>(
                    `/api/v1/review/dspy-variant-pr/next?limit=${limit}`,
                  ),
                staleTime: STALE_MS,
              });
            }

            export function useDspyVariantPrStats() {
              return useQuery({
                queryKey: [...DSPY_VARIANT_PR_KEY, 'stats'],
                queryFn: async () =>
                  _apiFetch<{
                    total: number;
                    unlabeled: number;
                    labeled_total: number;
                    label_accuracy: number | null;
                    accuracy_last_100: number | null;
                  }>('/api/v1/review/dspy-variant-pr/stats'),
                staleTime: STALE_MS,
              });
            }

            export function useLabelDspyVariantPr() {
              const qc = useQueryClient();
              return useMutation({
                mutationFn: async ({
                  id,
                  label,
                }: { id: string; label: DspyVariantPrLabelInput }) => {
                  const res = await fetch(
                    `/api/v1/review/dspy-variant-pr/${id}/label`,
                    {
                      method: 'POST',
                      headers: { 'content-type': 'application/json' },
                      body: JSON.stringify(label),
                    },
                  );
                  if (!res.ok) {
                    throw new Error(`label dspy-variant-pr failed: HTTP ${res.status}`);
                  }
                  return (await res.json()) as DspyVariantPrRow;
                },
                onMutate: async ({ id }) => {
                  await qc.cancelQueries({ queryKey: DSPY_VARIANT_PR_KEY });
                  const prev = qc.getQueryData<DspyVariantPrRow[]>([
                    ...DSPY_VARIANT_PR_KEY, 'next', 50,
                  ]);
                  qc.setQueryData<DspyVariantPrRow[] | undefined>(
                    [...DSPY_VARIANT_PR_KEY, 'next', 50],
                    (old) => old?.filter((r) => r.id !== id),
                  );
                  return { prev };
                },
                onError: (_err, _vars, ctx) => {
                  if (ctx?.prev) {
                    qc.setQueryData(
                      [...DSPY_VARIANT_PR_KEY, 'next', 50],
                      ctx.prev,
                    );
                  }
                },
                onSettled: () => {
                  qc.invalidateQueries({ queryKey: DSPY_VARIANT_PR_KEY });
                },
              });
            }

          Import the new type names at the top of queries.ts.

      (3) `services/dashboard/src/review/dspy_variant_pr.tsx` —
          the viewer page. Two-column layout mirroring
          `TriageLabeling.tsx`. Left column ("evidence"):
            - Header line with `judge_role`, `source_pr_number`
              (link to `source_pr_url`), `created_at`.
            - Two score badges: `current_score` and
              `variant_score`, with `improvement` formatted as
              `+0.073` or `-0.012`.
            - The `patch_diff` rendered in a `<pre>` with mono
              styling.
            - `corpus_s3_uri` shown as a labeled mono row.
          Right column ("LLM recommendation + label"):
            - LLM recommendation card: `llm_label`,
              `llm_confidence` badge, `llm_rationale` paragraph,
              `llm_prompt_version` + `llm_model` mono footer.
            - Label form: tri-state choice for `label_verdict`
              (merge / revise / drop / skip), free-text notes,
              an `override_reason` textarea that becomes required
              when the chosen verdict differs from `llm_label`.
              Submit button calls `useLabelDspyVariantPr().mutate`
              with `labeled_by: 'operator'` per the
              `TriageLabeling.tsx` constant.
          Empty state when the queue is empty mirrors
          `EmptyQueue` from `TriageLabeling.tsx`.

          The viewer is a default export so a future
          `import.meta.glob` registry (ADR-0070's framework
          substrate) can pick it up uniformly.

      (4) `services/dashboard/src/App.tsx` — add the new route:
            import DspyVariantPrReview from './review/dspy_variant_pr';
            ...
              <Route path="/review/dspy-variant-pr" element={<DspyVariantPrReview />} />
          Place it adjacent to the existing `/triage` route. Do
          NOT add it to `NAV` in PageLayout.tsx; per
          [[dashboard-triage-fixes plan]]'s precedent, NAV
          entries land separately when the surface is intended for
          discoverability — for this plan the URL is the v1
          contract.

      (5) `services/dashboard/src/review/dspy_variant_pr.test.tsx`:
          - `it('renders queue header + first row when data is
             present')` — render with a mocked
             `useDspyVariantPrQueue` returning one row; assert
             the judge_role + PR number render.
          - `it('renders the empty state when the queue is
             empty')` — mock returning `[]`; assert the
             EmptyQueue copy renders.
          - `it('submits a label with override_reason when the
             operator disagrees')` — mock fetch, render, click
             the "drop" choice (row has `llm_label='merge'`),
             type an override reason, click submit, assert
             fetch was called with the expected payload.
          - `it('blocks submit without override_reason when
             verdict disagrees with LLM')` — same scenario but
             no override text; assert the submit button is
             disabled or fetch is not called.
          (Mirror the wrapper/mocking pattern from
          `services/dashboard/src/api/queries.test.tsx`; per
          memory, vitest is NOT run inside the worker sandbox —
          the deterministic gate below only greps for the file
          + tested phrases. Real execution happens on the PR's
          CI step.)

      DOC:
        - `services/dashboard/AGENT.md` Recent-changes entry —
          one paragraph citing ADR-0070 substep 4 and the new
          `/review/dspy-variant-pr` route.

      OUT-OF-SCOPE:
        - `services/dashboard/src/pages/TriageLabeling.tsx`.
        - `services/dashboard/src/design/PageLayout.tsx`'s NAV.
        - Any API-side file (tasks 1 + 2 already merged).
        - `starters.py` (task 4).
    scope:
      files:
        - services/dashboard/src/review/dspy_variant_pr.tsx
        - services/dashboard/src/review/dspy_variant_pr.test.tsx
        - services/dashboard/src/api/queries.ts
        - services/dashboard/src/api/types.ts
        - services/dashboard/src/App.tsx
        - services/dashboard/AGENT.md
      services_affected:
        - services/dashboard
      out_of_scope:
        - services/dashboard/src/pages/TriageLabeling.tsx
        - services/dashboard/src/design/PageLayout.tsx
        - services/api/
    validation:
      - kind: deterministic
        description: |
          Viewer + test files exist; new route is registered;
          new hooks live in queries.ts; new types live in
          types.ts. vitest is not executed inside the sandbox
          (the dashboard's npm deps are absent); CI on the PR
          runs the suite.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/dashboard/src/review/dspy_variant_pr.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/dspy_variant_pr.test.tsx" ]
          grep -q "/review/dspy-variant-pr" "$ROOT/services/dashboard/src/App.tsx"
          grep -q "useDspyVariantPrQueue" "$ROOT/services/dashboard/src/api/queries.ts"
          grep -q "useLabelDspyVariantPr" "$ROOT/services/dashboard/src/api/queries.ts"
          grep -q "DspyVariantPrRow" "$ROOT/services/dashboard/src/api/types.ts"
          grep -q "DspyVariantPrLabelInput" "$ROOT/services/dashboard/src/api/types.ts"
      - kind: deterministic
        description: |
          The smoke test file declares four `it(...)` blocks (one
          per scenario) and each block has at least one `expect(`
          call (so the gate cannot pass on TODO-bodied stubs). The
          file imports vitest + the hook under test so the wiring
          is honest. Format-robust: counts and import-checks, not
          phrase greps on prose.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          FILE="$ROOT/services/dashboard/src/review/dspy_variant_pr.test.tsx"
          # Four `it(...)` declarations.
          [ "$(grep -c "^\s*it(" "$FILE")" -ge 4 ]
          # At least eight `expect(...)` calls — average two per scenario.
          [ "$(grep -c "expect(" "$FILE")" -ge 8 ]
          # Honest wiring: imports the hook + vitest + React testing-lib.
          grep -q "from 'vitest'" "$FILE"
          grep -q "useLabelDspyVariantPr" "$FILE"
          grep -q "@testing-library/react" "$FILE"
      - kind: deterministic
        description: |
          AGENT.md cites ADR-0070 and the new route.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "ADR-0070" "$ROOT/services/dashboard/AGENT.md"
          grep -q "/review/dspy-variant-pr" "$ROOT/services/dashboard/AGENT.md"

  - id: review-dspy-variant-pr-role
    title: "ADR-0070 substep 4.4 — role-dspy-variant-reviewer registration + prompt"
    workflow: wf-author
    # Sequenced behind task 2 (not task 1) because both tasks edit
    # the same `## Recent changes` paragraph in services/api/AGENT.md
    # added by task 1. Parallel execution would conflict on the
    # AGENT.md edit (and the per-task grep gates each only check
    # their own string, so a lost rebase wouldn't trip the gate).
    depends_on: [task.review-dspy-variant-pr-router.pr_merged]
    intent: |
      STUDY:
        - `services/api/treadmill_api/starters.py` — the `_ROLES`
          list around line 1100-1187. The `role-prompt-optimizer`
          entry (line ~1103) and the `role-ui-triage` entry
          (line ~1164) are both Sonnet-tier `OutputKind.ANALYSIS`
          roles; the new `role-dspy-variant-reviewer` follows the
          same shape. Note `role-ui-triage` uses
          `_load_prompt("role_ui_triage_v1.md")` to pull its full
          system prompt from a bundled artifact — mirror that for
          the new role.
        - `services/api/treadmill_api/prompts/role_ui_triage_v1.md`
          — the prompt-artifact shape (long-form markdown the
          role's system prompt is set from).
        - `services/api/tests/test_starters.py` — the role
          invariant tests around line 85+. Append a case for the
          new role.

      BUILD:

      (1) New prompt artifact at
          `services/api/treadmill_api/prompts/role_dspy_variant_reviewer_v1.md`:

          The prompt must instruct the LLM judge to:
            - Read a row's `judge_role`, `judge_prompt_path`,
              `current_score`, `variant_score`, `improvement`,
              `patch_diff`, and `corpus_s3_uri`.
            - Reason about whether the `patch_diff` is a sound
              refinement of the judge prompt, not just a higher-
              scoring one. Score-only improvements can be
              corpus-overfit; the judge must look at the diff
              and decide whether it's a real sharpening (one
              criterion clarified, one ambiguity removed, one
              missing failure mode added) or noise.
            - Emit one of three verdicts:
                merge — the diff is sound AND the improvement is
                        meaningful (>=0.05) AND the corpus is
                        large enough to trust the delta;
                revise — the direction is right but the patch
                         needs work (over-rewrites the prompt,
                         introduces a new ambiguity, etc.);
                drop — the patch is unsound (overfit, regression,
                       contradicts an existing criterion) or the
                       improvement is below threshold.
            - Confidence: high / medium / low per the operator's
              implicit calibration (low = the judge can't tell
              from the diff alone whether the change is sound;
              these are the rows the operator most wants to see).
            - Rationale: one paragraph citing specific lines of
              the diff.
          The structured-output envelope mirrors the
          `role-prompt-optimizer` schema in starters.py:
              {
                "verdict": "merge" | "revise" | "drop",
                "confidence": "high" | "medium" | "low",
                "rationale": "<one paragraph>"
              }
          Add the standard ADR-0055 footer ("No silent cross-
          account fallback; never paste secret values to chat").

      (2) `services/api/treadmill_api/starters.py` — append a new
          entry to the `_ROLES` list (immediately after the
          `role-ui-triage` entry around line 1182-1186). Shape:
            {
                # ADR-0070 substep 4. Sonnet-tier because the role
                # reasons about prompt design (the meta-output of
                # ADR-0053) and must emit a structured JSON
                # envelope. Rarely dispatched (Wave 4 cadence);
                # cost is not the relevant axis.
                "id": "role-dspy-variant-reviewer",
                "model": "claude-sonnet-4-6",
                "output_kind": OutputKind.ANALYSIS,
                "system_prompt": _load_prompt(
                    "role_dspy_variant_reviewer_v1.md"
                ),
            },
          Do NOT add a workflow definition referencing this role
          — workflow registration is out of scope for this plan
          (the role is dispatched on-demand via the same
          mechanism `role-prompt-optimizer` uses).

      (2b) `services/api/tests/test_starters.py` — update the THREE
          existing exact-match invariant constants so the wider
          test suite stays green. Adding `role-dspy-variant-reviewer`
          to `_ROLES` without these edits BREAKS
          `test_starters_declares_canonical_roles` (set-equality on
          `_EXPECTED_ROLE_IDS`), `test_role_model_tier_invariant`
          (the new role is Sonnet, so the else-branch fails),
          `test_seeded_roles_output_kinds_match_adr_0022` (the
          dict iteration silently SKIPS the new role — masks an
          ADR-0022 gap), and
          `test_every_role_is_referenced_by_at_least_one_workflow`
          (the new role is an orphan because this plan forbids
          adding a workflow).

          Required edits:

          (i) `_EXPECTED_ROLE_IDS` (~line 49-64): append
              `"role-dspy-variant-reviewer",  # ADR-0070 substep 4`.
          (ii) `_EXPECTED_OUTPUT_KINDS` (~line 167-181): append
               `"role-dspy-variant-reviewer": OutputKind.ANALYSIS,`.
          (iii) `SONNET_ROLES` inside `test_role_model_tier_invariant`
                (~line 245-250): append `"role-dspy-variant-reviewer",
                # ADR-0070 substep 4 — structured-output reasoner`.
          (iv) `test_every_role_is_referenced_by_at_least_one_workflow`
               (~line 647-656): exempt the new role from the orphan
               check by name. The role is dispatched on-demand (no
               workflow); exempt explicitly with a one-line comment
               citing ADR-0070 substep 4 and note that workflow
               registration lands in the operations follow-up. The
               canonical pattern: change the assertion from
               `assert not orphans` to
               `assert orphans == {"role-dspy-variant-reviewer"}` (or
               subtract the exempt set first). DO NOT register a
               workflow — workflow registration is explicitly OUT
               of scope.

      (3) `services/api/tests/test_starters.py` — append:
            def test_role_dspy_variant_reviewer_is_registered() -> None:
                """ADR-0070 substep 4: the proposing role for the
                dspy-variant-pr review queue is registered with the
                Sonnet model + ANALYSIS output kind, and its prompt
                artifact teaches the structured JSON envelope from
                ADR-0070 §"LLM recommendation"."""
                from treadmill_api.starters import _ROLES
                role = next(
                    (r for r in _ROLES if r["id"] == "role-dspy-variant-reviewer"),
                    None,
                )
                assert role is not None
                assert role["model"] == "claude-sonnet-4-6"
                assert role["output_kind"] == OutputKind.ANALYSIS
                prompt = role["system_prompt"]
                # Substantive prompt — bundled artifact, not a stub.
                assert len(prompt) >= 800, (
                    f"prompt should be a substantive multi-paragraph "
                    f"artifact, got {len(prompt)} chars"
                )
                # All three verdict values appear (mirrors
                # test_role_reviewer_prompt_teaches_json_envelope).
                for verdict in ("merge", "revise", "drop"):
                    assert verdict in prompt, (
                        f"prompt must teach verdict value {verdict!r}"
                    )
                # All three confidence values appear.
                for conf in ("high", "medium", "low"):
                    assert conf in prompt, (
                        f"prompt must teach confidence value {conf!r}"
                    )
                # JSON envelope structure (verdict + rationale +
                # confidence keys, plus the literal ```json fence).
                assert "```json" in prompt, (
                    "prompt must include a literal ```json fence so "
                    "the model has a concrete envelope template"
                )
                for key in ("verdict", "rationale", "confidence"):
                    assert key in prompt
                # Canonical inputs from the row schema.
                for input_name in ("judge_role", "corpus_s3_uri", "patch_diff"):
                    assert input_name in prompt, (
                        f"prompt must reference row input {input_name!r}"
                    )
                # ADR-0055 secrets-handling footer.
                assert "ADR-0055" in prompt or "silent cross-account" in prompt

          (Match imports + style with the existing tests around
          line 85 + 334.)

      DOC:
        - Extend the `services/api/AGENT.md` Recent-changes entry
          from task 1 to also cite the new role + prompt artifact.

      OUT-OF-SCOPE:
        - Workflow registration for the new role (the role is
          dispatched on-demand; workflow wiring is a later plan).
        - The corpus exporter cron.
        - The schedule that periodically fires the role.
        - Any router / viewer / model file (tasks 1-3).
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/treadmill_api/prompts/role_dspy_variant_reviewer_v1.md
        - services/api/tests/test_starters.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/routers/
        - services/api/treadmill_api/models/
        - services/api/treadmill_api/schemas/
        - services/dashboard/
    validation:
      - kind: deterministic
        description: |
          The new role + prompt artifact exist and the FULL
          test_starters.py suite passes — not just the new test.
          Running the whole file is load-bearing: adding the new
          role to `_ROLES` without updating `_EXPECTED_ROLE_IDS` /
          `SONNET_ROLES` / `_EXPECTED_OUTPUT_KINDS` / the orphan
          check breaks three sibling tests. The narrow-test-only
          gate from earlier drafts let those slip past the
          sandbox.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/treadmill_api/prompts/role_dspy_variant_reviewer_v1.md" ]
          grep -q "role-dspy-variant-reviewer" "$ROOT/services/api/treadmill_api/starters.py"
          grep -q "role_dspy_variant_reviewer_v1.md" "$ROOT/services/api/treadmill_api/starters.py"
          # Full file — catches missed updates to _EXPECTED_ROLE_IDS,
          # _EXPECTED_OUTPUT_KINDS, SONNET_ROLES, orphan-role check.
          cd "$ROOT/services/api" && uv run pytest tests/test_starters.py -v
      - kind: deterministic
        description: |
          AGENT.md cites the new role.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "role-dspy-variant-reviewer" "$ROOT/services/api/AGENT.md"
```

## Risks / unknowns

- **Substrate dependency on substep 1.** This plan now consumes
  substep 1's substrate directly: the ORM inherits from
  `ReviewQueueRowMixin + Base`, the router lives under
  `routers/review/` so the substep-1 aggregator picks it up, and
  the viewer plugs into the substep-1 chrome via the per-kind
  registry. If substep 1 hasn't landed at dispatch time, task 1
  STOPs in its STUDY block and surfaces the gap — do NOT author
  the row class against a moving mixin. The first commit in this
  plan should be a one-line preflight check that confirms
  `ReviewQueueRowMixin` exists at
  `services/api/treadmill_api/models/review_queue_row.py` (or
  wherever substep 1 puts it; the worker reads substep 1's actual
  doc_path to confirm).
- **Override-reason cross-check duplication.** The override-reason
  rule lives in *two* places: the router's POST handler (server-side
  authority) and the viewer's submit-button enabled state (client-side
  UX). They can drift. Mitigation: a server-side 422 is the source of
  truth — the dashboard test covers the disabled-submit case, but if
  the operator bypasses it the router still rejects. The
  `test_post_label_requires_override_reason_when_disagreeing` test in
  task 2 pins server-side authority.
- **Numeric / Decimal at the JSON seam — DECIDED: float.** Score
  columns are `Numeric(5,4)` in Postgres so precision survives on
  write. The Pydantic schema declares scores as `float` so
  `.model_dump_json()` emits an unquoted JSON number (not a quoted
  Decimal string — Pydantic v2's default for `Decimal` is to emit a
  JSON string for safety, which would break the TS hook that reads
  `number`). SQLAlchemy coerces Numeric → float at the ORM seam on
  read. Task 1's `test_pydantic_score_json_round_trip` pins this:
  the JSON output substring matches `0.7543` (NOT `"0.7543"`); the
  round-trip back via `model_validate_json` returns an equal float.
- **Confidence ordering semantics.** ADR-0070 says "least confident
  first" — for `low | medium | high` confidence labels, "low
  confidence" means the LLM is least sure, so those rows are highest
  leverage for the operator to label. The router orders
  `low->medium->high` ascending, which surfaces low-confidence rows
  first. Documented in task 2's intent so the worker doesn't invert
  it.
- **Worker-sandbox vitest exclusion.** The dashboard tests in task 3
  are NOT executed by the worker (per
  [[feedback-worker-validation-script-scope]] + the
  `2026-06-02-dashboard-triage-fixes` precedent). The deterministic
  gate only greps for the test file + scenario names. Real
  execution lives on the PR's CI. If a regression slips through
  grep-only gating, the architect's review will catch it via the
  CI annotation.
- **Migration ordering against in-flight plans.** Today's head on
  main is `20260604_0100`. If another plan lands a migration before
  this task 1 merges, the new revision needs a rebased
  `down_revision`. Mitigation: task 1's depends_on is empty (it's
  the root of this plan), so the worker is dispatched first; if a
  rebase is needed the architect's amend loop catches it via the
  `alembic heads` CI gate.
- (Decimal-vs-float risk is consolidated into the "Numeric / Decimal
  at the JSON seam" entry above — the decision is `float` in the
  Pydantic schema, with the JSON round-trip pinned by
  `test_pydantic_score_json_round_trip`.)

## Diagram

Not applicable. ADR-0070 carries the canonical cybernetic-loop
diagram this plan implements one kind of.

## Decisions captured during execution

(empty at draft time)

## Post-mortem

(filled when plan transitions to completed)
