---
auto_merge: true
status: active
---

# Plan: ADR-0070 substep 1 — Framework substrate (ReviewQueueRowMixin + dashboard chrome + viewer registry)

- **Status:** active
- **Date:** 2026-06-04
- **Related ADRs:**
  - ADR-0070 (the decision being implemented — pre-labeled review queues
    as a Treadmill primitive)
  - ADR-0061 (the precedent — `TriageFindingRow` is the row shape every
    kind will inherit from)
  - ADR-0056 (the auto-discovery seam — both the per-kind API routers
    and the per-kind viewers ride on `pkgutil.iter_modules` /
    `import.meta.glob` patterns this ADR already established)
  - ADR-0027 (the per-row LLM-recommendation envelope every kind's
    `llm_*` columns conform to)

## Goal

Ship the substrate that every future review-queue kind will conform
to, with **no new tables** and **no refactor of the existing triage
surface**. After this plan lands a future kind is mechanically two
files (router + viewer) + one migration + one role — every shared
behavior (six-layer mixin, accuracy-stats aggregation, flip-through
chrome, keyboard handler, confidence-bucket strip, per-kind viewer
registry) lives in this substrate and is exercised by tests that
don't touch any specific kind.

The endpoint of this substep is the abstraction proven by **its own**
behavioral tests (a fake kind built solely inside the test suite), not
by any production surface. Substep 2 promotes triage onto it; substep
3 ships the first new kinds.

## Success criteria

1. A Python module `services/api/treadmill_api/models/review_queue.py`
   exports `ReviewQueueRowMixin` — a SQLAlchemy `mapped_column`-based
   mixin enforcing the six-layer shape from ADR-0070 (provenance,
   candidate content as abstract per-kind columns, LLM recommendation
   with closed `llm_confidence` enum, operator label fields,
   `labeled_*` metadata, and outcome). `mypy`-checked.
2. A Python module `services/api/treadmill_api/routers/review/base.py`
   exposes `build_review_router(prefix, row_cls, label_input_model,
   verdict_attr)` — a factory that returns an `APIRouter` mounting the
   four mandatory endpoints (`GET /next`, `GET /{id}`, `POST
   /{id}/label`, `GET /stats`) per ADR-0070. The aggregator
   `routers/review/__init__.py` follows the existing
   `pkgutil.iter_modules` auto-discovery pattern (mirrors
   `routers/dashboard/__init__.py` and `routers/triage/__init__.py`),
   mounted at `/api/v1/review` by `app.py`.
3. `services/api/treadmill_api/services/review_stats.py` exposes
   `compute_stats(session, row_cls, verdict_attr) -> StatsResponse`
   with `total`, `unlabeled`, `labeled_total`, `label_accuracy`, and
   `accuracy_last_100`. Accuracy is the fraction of labeled rows where
   the operator's verdict matched the LLM's recommendation, ignoring
   `NULL` operator verdicts (skip = not-an-answer).
4. `services/dashboard/src/review/` ships the shared chrome:
   `FlipThroughLayout.tsx`, `useReviewKeyboard.ts`, `ConfidenceStrip.tsx`,
   `types.ts` (the `ReviewKindViewerProps` + `ReviewLabelInput` contracts
   the substrate depends on), and `registry.ts` using
   `import.meta.glob('./viewers/*.tsx', { eager: true })` to wire
   per-kind viewers by filename.
5. A single new dashboard route `/review/:kind` (added to
   `services/dashboard/src/App.tsx`) mounts the framework on whatever
   viewer the registry resolves. A 404 fallback renders when `kind` is
   unknown.
6. **No production kind is added in this plan.** Tests exercise the
   substrate against synthetic in-test row classes and viewer modules
   (mirrors the synthetic-sibling tests in
   `tests/test_routers_dashboard_init.py`).
7. AGENT.md updates on `services/api` and `services/dashboard`
   referencing ADR-0070 and listing the new modules.

## Constraints / scope

### In scope

- `ReviewQueueRowMixin` (SQLAlchemy mixin, no `__tablename__` — kinds
  pick their own).
- `build_review_router` factory + `routers/review/__init__.py`
  auto-discovery package.
- `compute_stats` helper + its `StatsResponse` Pydantic model.
- Dashboard substrate: `FlipThroughLayout`, `useReviewKeyboard`,
  `ConfidenceStrip`, `registry.ts`, one route entry, viewer-contract
  TypeScript types.
- AGENT.md updates on the two affected services.

### Out of scope

- **Any new per-kind table or migration.** `triage_findings` stays
  exactly where it is; no migrations land in this plan.
- **Refactor of `routers/triage/labels.py` or `TriageLabeling.tsx`.**
  Substep 2 (a separate plan) moves triage onto the substrate. Touching
  it here would invalidate the "no production-surface drift" guarantee.
- **The corpus exporter, the proposing-role scheduling, the
  retrospective scorer.** All downstream of substep 3+.
- **Any new JSONB column.** Per the architecture's three-site rule;
  the mixin uses typed columns only.
- **Cross-kind dashboards.** ADR-0070 explicitly defers them to v2.
- **Authentication / RBAC on the new endpoints.** Same posture as
  `routers/triage`; the dashboard sits behind the operator network
  already.

### Budget

Four worker dispatches. Sequential `depends_on` between Task 1 (mixin)
and Task 2 (router factory) because Task 2 imports the mixin's enum
type. Task 3 (dashboard chrome) is independent of Tasks 1+2 (the
substrate's TypeScript types are local to the React side). Task 4
wires the new route + AGENT.md updates and depends on 1+2+3. If any
task wedges at the architect cap, investigate before the next ships.

## Sequence of work

```yaml
sequence_of_work:
  - id: review-queue-row-mixin
    title: "ADR-0070 substep 1.1 — ReviewQueueRowMixin + tests"
    workflow: wf-author
    intent: |
      STUDY:
        - `services/api/treadmill_api/models/triage_finding.py` is the
          precedent. The mixin generalizes its six layers: provenance
          (lines 35-50), candidate content (lines 52-69 — these are
          per-kind so the mixin doesn't enforce shape, only the
          `source_run_id` anchor + `source_url` + `source_pr_number`
          nullables), LLM recommendation (the existing `prompt_version`
          + `model` + `confidence` columns are the model; generalize as
          `llm_prompt_version`, `llm_model`, `llm_confidence` with a
          closed CHECK), operator label (`label_*` columns 108-121 —
          generalize the metadata fields, leave the verdict column to
          per-kind subclasses since each kind has its own enum), and
          outcome (lines 95-105).
        - `services/api/treadmill_api/database.py` — `Base` is
          `DeclarativeBase`. The mixin is a plain `MixinClass` (no
          `__tablename__`); subclasses combine it with `Base` in their
          own `models/<kind>.py`.
        - `services/api/treadmill_api/models/__init__.py` — keep
          imports current; add `ReviewQueueRowMixin` to it.

      BUILD `services/api/treadmill_api/models/review_queue.py`:
        - `class ReviewQueueRowMixin:` (NOT inheriting `Base`). Uses
          SQLAlchemy 2.0 `Mapped[...] = mapped_column(...)` syntax
          identical to `TriageFindingRow`.
        - **Provenance layer** (always present, mixin-enforced):
          * `id: Mapped[uuid.UUID]` PK, server-default
            `gen_random_uuid()`.
          * `created_at: Mapped[datetime]` server-default `now()`,
            non-nullable, timezone-aware.
          * `source_run_id: Mapped[uuid.UUID | None]` nullable (not
            every kind anchors to a workflow run).
          * `source_event_id: Mapped[uuid.UUID | None]` nullable.
          * `source_url: Mapped[str | None]` nullable Text.
          * `source_pr_number: Mapped[int | None]` nullable Integer.
        - **LLM recommendation layer** (enforced):
          * `llm_confidence: Mapped[str]` `String(8)`, non-null.
          * `llm_rationale: Mapped[str]` Text, non-null.
          * `llm_prompt_version: Mapped[str]` Text, non-null.
          * `llm_model: Mapped[str]` Text, non-null.
          * The `llm_label` column is per-kind (each kind has its own
            verdict enum) — mixin documents this contract in its
            docstring; subclasses must provide it. We do NOT declare
            a name-collision-prone column here.
        - **Operator-label metadata layer** (enforced):
          * `label_notes: Mapped[str | None]` Text nullable.
          * `label_override_reason: Mapped[str | None]` Text nullable
            (per ADR-0070; new field beyond ADR-0061's shape).
          * `labeled_by: Mapped[str | None]` Text nullable.
          * `labeled_at: Mapped[datetime | None]` TIMESTAMP nullable.
          * `label_guidelines_version: Mapped[str | None]` Text
            nullable.
        - **Outcome layer** (optional but typed):
          * `outcome_state: Mapped[str | None]` `String(16)` nullable.
          * `outcome_pr_merged_at: Mapped[datetime | None]` TIMESTAMP
            nullable.
        - Class-level constant `LLM_CONFIDENCE_VALUES = ("high",
          "medium", "low")` (frozenset/tuple) so subclasses can build
          a CHECK constraint by importing it.
        - Class-level constant `OUTCOME_STATE_VALUES = ("pending",
          "merged", "rejected", "superseded", "cancelled")` mirroring
          ADR-0061's outcome enum.
        - Helper `@classmethod def review_queue_check_constraints(
          cls, *, table_name: str) -> tuple[CheckConstraint, ...]`
          returning the two closed-enum CHECKs the subclass appends to
          its `__table_args__` (matching the naming convention
          `ck_<table>_llm_confidence`, `ck_<table>_outcome_state`).
        - Helper `@classmethod def unlabeled_index(cls, *, table_name:
          str, verdict_column: str) -> Index` returning the partial
          index `ix_<table>_unlabeled` keyed by `<verdict_column> IS
          NULL`. Mirrors the ADR-0061 pattern at
          `models/triage_finding.py` lines 163-167.

      Tests (`services/api/tests/test_models_review_queue.py`):
        - Behavioral test pinning the mixin shape: assemble a
          synthetic subclass `class _FakeKindRow(ReviewQueueRowMixin,
          Base): __tablename__ = "_fake_review_kind_mixin_test"`
          (the test-module-distinct `__tablename__` prevents a
          `Table already defined for this MetaData` collision when
          pytest collects Task 2's `_FakeKindRow` in the same
          process). With a
          `llm_label: Mapped[str]` enum-typed verdict column AND a
          `label_verdict: Mapped[str | None]` operator column AND a
          per-kind candidate column (`candidate_text: Mapped[str]`).
          Apply the helper-built CHECKs + index in `__table_args__`.
          Assert: `_FakeKindRow.__table__` carries the six expected
          columns from the mixin, the two CHECKs by name, and the
          partial index by name.
        - Negative: a subclass that omits `__tablename__` raises
          (the mixin doesn't supply one; SQLAlchemy errors at
          declarative-class build time).
        - Assert `LLM_CONFIDENCE_VALUES` is `("high", "medium",
          "low")` and `OUTCOME_STATE_VALUES` matches ADR-0061's
          enum so future kinds can't drift.
        - Assert `unlabeled_index(...)` produces an index whose
          `postgresql_where` clause exactly references the supplied
          verdict column (`text("<verdict_column> IS NULL")`).

      AGENT.md update on `services/api` listing the new module and
      noting that ADR-0070 kinds inherit from this mixin (not from
      `TriageFindingRow`).
    scope:
      files:
        - services/api/treadmill_api/models/review_queue.py
        - services/api/treadmill_api/models/__init__.py
        - services/api/tests/test_models_review_queue.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/models/triage_finding.py
        - services/api/treadmill_api/triage_store.py
        - services/api/alembic
        - services/api/treadmill_api/routers
    validation:
      - kind: deterministic
        description: |
          The new mixin module exists and is importable; the
          behavioral tests for the mixin pass.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/treadmill_api/models/review_queue.py" ]
          [ -f "$ROOT/services/api/tests/test_models_review_queue.py" ]
          cd "$ROOT/services/api" && uv run pytest tests/test_models_review_queue.py -q
      - kind: deterministic
        description: |
          The triage row + its tests are untouched (no in-scope drift).
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          cd "$ROOT/services/api" && uv run pytest tests/test_routers_triage_labels.py -q
      - kind: deterministic
        description: |
          AGENT.md references ADR-0070 and the new module.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "ADR-0070" "$ROOT/services/api/AGENT.md"
          grep -qE "review_queue|ReviewQueueRowMixin" "$ROOT/services/api/AGENT.md"

  - id: review-queue-router-factory
    title: "ADR-0070 substep 1.2 — build_review_router factory + auto-discovery package"
    workflow: wf-author
    depends_on: [task.review-queue-row-mixin.pr_merged]
    intent: |
      STUDY:
        - `services/api/treadmill_api/routers/dashboard/__init__.py`
          and `services/api/treadmill_api/routers/triage/__init__.py`
          are the auto-discovery template. The new
          `routers/review/__init__.py` follows the SAME pattern:
          aggregator at `/api/v1/review`, `pkgutil.iter_modules`
          discovery, `MOUNTED_MODULES` list, and a guard test that
          asserts the file does NOT enumerate any sibling by name.
        - `services/api/treadmill_api/routers/triage/labels.py` is
          the closest endpoint precedent. The factory generalizes its
          `GET /findings` (becomes `GET /next`), `POST /findings/{id}/
          label` (becomes `POST /{id}/label`), and adds the two
          missing endpoints (`GET /{id}`, `GET /stats`).
        - `services/api/treadmill_api/app.py` line ~304 mounts the
          triage router; add `review_router` import + include after it.

      BUILD `services/api/treadmill_api/routers/review/__init__.py`:
        - Copy the auto-discovery shape from `routers/triage/__init__.py`
          verbatim, change the prefix to `/api/v1/review`, change the
          tag to `review`.

      BUILD `services/api/treadmill_api/routers/review/base.py` (the
      factory; this file is NOT auto-discovered as an endpoint module —
      it doesn't define a module-level `router` named `router`. Name
      the factory function `build_review_router` so the discovery loop
      ignores it; the loop only mounts modules where `getattr(module,
      "router", None)` is an `APIRouter`. We deliberately do NOT
      assign a module-level `router` in base.py.):
        - `class StatsResponse(BaseModel)`: `total: int`, `unlabeled:
          int`, `labeled_total: int`, `label_accuracy: float | None`,
          `accuracy_last_100: float | None`. Accuracy is None when
          `labeled_total == 0` (no denominator).
        - `def build_review_router(*, prefix: str, row_cls: type,
          label_input_model: type[BaseModel], verdict_attr: str,
          llm_label_attr: str = "llm_label") -> APIRouter`:
          * `prefix`: full path prefix (e.g. `/architect-gold`). The
            factory mounts it as `router = APIRouter(prefix=prefix)`.
          * `row_cls`: the SQLAlchemy row class (subclass of
            `ReviewQueueRowMixin + Base`).
          * `label_input_model`: a per-kind Pydantic `BaseModel` whose
            `model_dump()` is splatted onto the row.
          * `verdict_attr`: name of the column that the operator's
            verdict is written to (`label_verdict` etc.).
          * `llm_label_attr`: defaults to `"llm_label"` — name of the
            column the LLM's recommendation lives in for accuracy math.
        - Endpoints created on the returned router. **Register the
          literal-path routes (`/next`, `/stats`) BEFORE the
          parameterized routes (`/{id}`, `/{id}/label`)** on the
          APIRouter — FastAPI matches routes in registration order,
          so swapping these would route `/stats` to the `/{id}`
          handler with `id="stats"` and produce a 422 (UUID parse
          error) instead of stats:
          * `GET /next?limit=N` (default 20, max 100): query rows
            where `getattr(row_cls, verdict_attr) IS NULL` ORDER BY
            `llm_confidence ASC, created_at ASC`. Confidence ordering
            uses a CASE expression so `low` < `medium` < `high`
            (deterministic).
          * `GET /stats`: delegate to `review_stats.compute_stats` and
            return `StatsResponse`.
          * `GET /{id}`: fetch one row by PK; 404 when missing.
          * `POST /{id}/label`: load by id (404 if missing), update
            the verdict column + label metadata from the input model,
            stamp `labeled_at = now()`. Body MUST include
            `labeled_by`; rest are optional. Return the refreshed row
            as a Pydantic-serialized dict.
        - Each endpoint's `response_model` is constructed dynamically
          using `pydantic.create_model` against `row_cls.__table__.columns`
          (so the wire shape is the row's typed columns) OR the
          factory accepts an `output_model` parameter — pick the
          simpler path: accept `output_model: type[BaseModel]` so the
          per-kind module owns the wire schema.
        - Signature is therefore: `build_review_router(*, prefix:
          str, row_cls: type, label_input_model: type[BaseModel],
          output_model: type[BaseModel], verdict_attr: str,
          llm_label_attr: str = "llm_label") -> APIRouter`.

      BUILD `services/api/treadmill_api/services/__init__.py` (empty
      file; this sub-package is new — `treadmill_api/services/` does
      not exist yet, and without `__init__.py` the import
      `from treadmill_api.services.review_stats import compute_stats`
      raises `ModuleNotFoundError` at test-collection time).

      BUILD `services/api/treadmill_api/services/review_stats.py`:
        - `async def compute_stats(session: AsyncSession, *,
          row_cls: type, verdict_attr: str, llm_label_attr: str =
          "llm_label") -> StatsResponse`.
        - Three queries (all parameterized on `row_cls.__tablename__`
          via the SQLAlchemy ORM, never raw SQL):
          1. `total = SELECT COUNT(*) FROM <table>`.
          2. `unlabeled = SELECT COUNT(*) FROM <table> WHERE
             <verdict_attr> IS NULL`.
          3. `labeled_total = total - unlabeled`.
          4. `label_accuracy`: SELECT COUNT(*) where
             `<verdict_attr> IS NOT NULL AND <verdict_attr> =
             <llm_label_attr>`, divided by `labeled_total`. None when
             labeled_total == 0.
          5. `accuracy_last_100`: same fraction but bounded to the
             most recent 100 labeled rows (ORDER BY `labeled_at DESC
             LIMIT 100` as a subquery, then COUNT match / 100 — but
             use the actual subquery size as denominator when fewer
             than 100 labeled rows exist).
        - All queries go through `session.scalar(select(func.count()))`
          / equivalent SQLAlchemy ORM. No raw SQL strings.

      In `services/api/treadmill_api/app.py`: import `review_router`
      from `treadmill_api.routers.review`, add it to the
      `app.include_router(...)` block just after `triage_router`.

      Tests (`services/api/tests/test_routers_review_base.py`):
        - Build a tiny synthetic `_FakeKindRow(ReviewQueueRowMixin,
          Base)` table for the test session with
          `__tablename__ = "_fake_review_kind_router_test"`
          (distinct from Task 1's `_fake_review_kind_mixin_test` so
          both modules can coexist in the same pytest process without
          colliding on `Base.metadata`). Mirrors the stub-session
          approach in `test_routers_triage_labels.py`. Use a stub
          session that yields fixture rows; do NOT spin up Postgres.
        - Build the factory output `router = build_review_router(
          prefix="/_fake-kind", row_cls=_FakeKindRow, ...)` and mount
          on a `FastAPI()` app.
        - Test cases (each is its own `def test_...()`):
          1. `GET /api/v1/review/_fake-kind/next` returns the
             configured row list, ordered by confidence ASC then
             created_at ASC. Assert at least one ordering case with
             three rows (low/medium/high confidences).
          2. `GET /api/v1/review/_fake-kind/next?limit=N` honors N.
          3. `GET /api/v1/review/_fake-kind/{id}` returns the row
             when present, 404 when missing.
          4. `POST /api/v1/review/_fake-kind/{id}/label` 200s,
             writes the verdict column + metadata, and the row no
             longer appears in `GET /next`.
          5. `POST .../label` 404 when id is unknown.
          6. `POST .../label` without `labeled_by` → 422 (the input
             model's required field).
          7. `GET /api/v1/review/_fake-kind/stats` returns
             `StatsResponse` shape — when no rows labeled,
             `label_accuracy` is `None`; when 4 of 5 labeled rows
             match the LLM verdict, `label_accuracy` is `0.8`.
      Tests (`services/api/tests/test_services_review_stats.py`):
        - Dedicated unit tests for `compute_stats` independent of
          the HTTP layer. Use the same synthetic `_FakeKindRow`
          subclass (with the Task 2 `__tablename__`) + a stub
          session that returns scripted scalar values per query.
          Case 1: empty table → `total=0`, `unlabeled=0`,
          `labeled_total=0`, `label_accuracy=None`,
          `accuracy_last_100=None` (None denominator when nothing
          labeled).
          Case 2: 10 rows, 4 unlabeled, 6 labeled where 5 match
          LLM → `total=10`, `unlabeled=4`, `labeled_total=6`,
          `label_accuracy=5/6`.
          Case 3: fewer than 100 labeled rows → `accuracy_last_100`
          uses the actual subquery row count as denominator (not a
          hardcoded 100). E.g. 12 labeled, 9 match → `9/12`.
          Case 4: NULL operator verdict (skip) is excluded from
          the accuracy numerator AND denominator (skip = not an
          answer).

        Tests (`services/api/tests/test_routers_review_init.py`):
          1. The aggregator router is prefixed `/api/v1/review` and
             tagged `review`.
          2. Source-level assertion that
             `routers/review/__init__.py` does NOT mention `base`
             (and base.py doesn't expose a module-level `router`).
          3. Synthetic-sibling test mirroring
             `test_routers_dashboard_init.py::test_discovery_picks_up_a_freshly_added_sibling`
             — drop a tmp sibling that defines `router = APIRouter()`
             with one route, assert discovery picks it up.
          4. `test_app_mounts_review_router_once` — assert the app
             carries the prefix and only includes the aggregator
             once.

      AGENT.md updates on `services/api` listing the new `routers/review`
      package, the `services/review_stats.py` helper, and the four
      mandatory endpoints per ADR-0070.
    scope:
      files:
        - services/api/treadmill_api/routers/review/__init__.py
        - services/api/treadmill_api/routers/review/base.py
        - services/api/treadmill_api/services/__init__.py
        - services/api/treadmill_api/services/review_stats.py
        - services/api/treadmill_api/app.py
        - services/api/tests/test_routers_review_base.py
        - services/api/tests/test_routers_review_init.py
        - services/api/tests/test_services_review_stats.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/routers/triage
        - services/api/treadmill_api/routers/dashboard
        - services/api/treadmill_api/models/triage_finding.py
        - services/api/treadmill_api/triage_store.py
    validation:
      - kind: deterministic
        description: |
          The new substrate modules exist + the factory + stats tests
          pass. The `services/` Python sub-package is new — the
          `__init__.py` existence check catches a missing-package
          omission before pytest's ModuleNotFoundError.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/treadmill_api/routers/review/__init__.py" ]
          [ -f "$ROOT/services/api/treadmill_api/routers/review/base.py" ]
          [ -f "$ROOT/services/api/treadmill_api/services/__init__.py" ]
          [ -f "$ROOT/services/api/treadmill_api/services/review_stats.py" ]
          cd "$ROOT/services/api" && uv run pytest tests/test_routers_review_base.py tests/test_routers_review_init.py tests/test_services_review_stats.py -q
      - kind: deterministic
        description: |
          The existing triage + dashboard tests still pass — substrate
          must not regress the production surfaces.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          cd "$ROOT/services/api" && uv run pytest tests/test_routers_triage_labels.py tests/test_routers_dashboard_init.py -q
      - kind: deterministic
        description: |
          app.py mounts the new aggregator. Accepts either of the
          repo's two router-import idioms (`from treadmill_api.routers.review import router as review_router`
          which is the established convention per app.py:34-53, or
          `from treadmill_api.routers import review`).
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -qE "from treadmill_api\.routers\.review|from treadmill_api\.routers import review" "$ROOT/services/api/treadmill_api/app.py"
          grep -q "/api/v1/review\|review_router\|review\.router" "$ROOT/services/api/treadmill_api/app.py"
      - kind: deterministic
        description: |
          AGENT.md references ADR-0070 + the new package and helper.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "ADR-0070" "$ROOT/services/api/AGENT.md"
          grep -qE "routers/review|build_review_router" "$ROOT/services/api/AGENT.md"
          grep -qE "review_stats|compute_stats" "$ROOT/services/api/AGENT.md"

  - id: review-dashboard-chrome
    title: "ADR-0070 substep 1.3 — shared dashboard chrome (FlipThroughLayout + keyboard handler + ConfidenceStrip)"
    workflow: wf-author
    intent: |
      STUDY:
        - `services/dashboard/src/pages/TriageLabeling.tsx` — the
          existing flip-through page is the visual + interaction
          template. The shared chrome generalizes its layout (header,
          evidence column on the left, label column on the right,
          submit-and-next flow) into a reusable component without
          touching the triage page itself.
        - `services/dashboard/src/design/PageLayout.tsx` —
          `PageLayout` is the outer chrome the new component wraps.
        - `services/dashboard/vite.config.ts` confirms Vite + Vitest
          + jsdom test setup; `package.json` confirms React 19 +
          react-router-dom 7.
        - `services/dashboard/src/api/queries.ts` — pattern for
          per-page tanstack-query hooks. The new chrome accepts hook
          values as props rather than calling hooks internally
          (testability).

      BUILD `services/dashboard/src/review/types.ts`:
        - `export type ReviewConfidence = 'high' | 'medium' | 'low';`
        - `export interface ReviewLlmRecommendation { label: string;
          confidence: ReviewConfidence; rationale: string;
          prompt_version: string; model: string; }`
        - `export interface ReviewLabelInput { label: string;
          override_reason?: string | null; notes?: string | null;
          labeled_by: string; }`
        - `export interface ReviewRow<TCandidate, TLlm extends string>
          { id: string; created_at: string; source_url?: string |
          null; source_pr_number?: number | null; candidate:
          TCandidate; llm: ReviewLlmRecommendation; }` (TLlm is
          phantom-used to constrain per-kind label enums).
        - `export interface ReviewKindViewerProps<TCandidate, TLlm
          extends string> { row: ReviewRow<TCandidate, TLlm>; onLabel:
          (input: ReviewLabelInput) => void; }`
        - `export type ReviewKindViewer<TCandidate = unknown, TLlm
          extends string = string> = (props: ReviewKindViewerProps<
          TCandidate, TLlm>) => React.ReactElement;`

      BUILD `services/dashboard/src/review/useReviewKeyboard.ts`:
        - `interface KeyHandlers { onAccept: () => void; onReject: ()
          => void; onSkip: () => void; onHelp: () => void; onNext:
          () => void; onPrev: () => void; }`
        - `export function useReviewKeyboard(handlers:
          KeyHandlers, *, enabled: boolean = true): void`. Uses
          `useEffect` + `window.addEventListener('keydown', ...)`.
          Keys: `space` → `onAccept`; `x` → `onReject`; `s` →
          `onSkip`; `?` → `onHelp`; `j` → `onNext`; `k` → `onPrev`.
          Calls `event.preventDefault()` for handled keys.
          Guard: ignore the event when `document.activeElement` is
          an `INPUT`, `TEXTAREA`, or `[contenteditable]` so typing
          into the notes field doesn't trigger shortcuts.

      BUILD `services/dashboard/src/review/ConfidenceStrip.tsx`:
        - `interface ConfidenceCount { confidence: ReviewConfidence;
          labeled_today: number; queue_remaining: number; }`
        - `interface ConfidenceStripProps { counts:
          ConfidenceCount[]; accuracyToday?: number | null; }`
        - Renders a single-row strip with three buckets (high /
          medium / low), each showing labeled-today and
          queue-remaining counts. When `accuracyToday` is provided,
          renders a sibling pill with the percentage.
        - Pure presentation — no hooks, no fetches.

      BUILD `services/dashboard/src/review/FlipThroughLayout.tsx`:
        - Props: `{ title: string; row: ReviewRow<unknown, string> |
          null; onLabel: (input: ReviewLabelInput) => void;
          remaining: number; viewer: ReviewKindViewer; loading:
          boolean; error: Error | null; stats: { counts:
          ConfidenceCount[]; accuracyToday: number | null } | null;
          }`.
        - Layout:
          1. `<PageLayout>` wrapper (same as `TriageLabeling`).
          2. Top: `<ConfidenceStrip />` driven by `stats`.
          3. Body: the per-kind `viewer` invoked with `{row,
             onLabel}` props.
          4. When `row` is null + not loading: empty-queue message
             ("// queue empty / nothing to label here").
          5. Wires `useReviewKeyboard` mapped to:
             * `onAccept` → invokes `onLabel({label: row.llm.label,
               labeled_by: 'operator'})` (the one-keystroke confirm
               path per ADR-0070).
             * `onReject` → focuses the per-viewer override-reason
               field by dispatching a custom event
               `review:request-override-focus`. Per-kind viewers can
               choose to listen.
             * `onSkip` → invokes `onLabel({label: '__skip__', ...})`
               — TODO marker; we don't yet have the skip semantics
               wired; for now `onSkip` calls a prop callback
               `onSkip?: () => void` instead of dispatching a label
               write. Update prop interface: add `onSkip?: () =>
               void; onShowHelp?: () => void;`
             * `onNext/onPrev` → no-op for v1 (queue auto-advances on
               label). Document as deferred.

      BUILD `services/dashboard/src/review/registry.ts`:
        - `const modules = import.meta.glob<{ default:
          ReviewKindViewer }>('./viewers/*.tsx', { eager: true });`
        - Builds a `Map<string, ReviewKindViewer>` keyed on the
          filename stem (e.g. `./viewers/architect-gold.tsx` →
          `architect-gold`).
        - `export function getViewer(kind: string): ReviewKindViewer
          | null`. Returns null when the kind isn't registered.
        - `export function listKinds(): string[]`.
        - Create the `services/dashboard/src/review/viewers/`
          directory and add a single placeholder `_README.txt` (NOT
          a `.tsx` file — must not be discovered) explaining the
          contract for future kinds. This task does NOT add any real
          per-kind viewer; the registry returns null on every lookup
          until substep 2 lands the first one.

      Tests (`services/dashboard/src/review/*.test.tsx` and
      `*.test.ts`):
        - `useReviewKeyboard.test.tsx`:
          * Render a tiny harness component using `@testing-library/
            react`; spy on each handler; dispatch keyboard events;
            assert mappings. Cover the input-focus guard: when an
            `<input>` has focus and 'space' is pressed, `onAccept`
            does NOT fire. Cover the `enabled: false` case.
        - `ConfidenceStrip.test.tsx`:
          * Render with three buckets, assert all three labels render
            with their counts.
          * Render with `accuracyToday: 0.87`, assert "87%" appears.
          * Render with `accuracyToday: null`, assert the accuracy
            pill is not rendered.
        - `FlipThroughLayout.test.tsx`:
          * Render with `row: null, loading: false`, assert
            "queue empty" copy.
          * Render with a stub viewer that displays the row's `id`;
            assert the id is on the screen.
          * Press 'space' (jsdom keyboard event), assert the supplied
            `onLabel` is called with `{label: row.llm.label,
            labeled_by: 'operator'}`.
          * Render with `error: new Error('boom')`, assert
            "boom" is surfaced.
        - `registry.test.ts`:
          * `getViewer('does-not-exist')` returns null.
          * `listKinds()` returns the (currently empty) string list.
          * Mock `import.meta.glob` by injecting a fake glob result
            via Vitest's `vi.mock(...)` if the test framework cannot
            see real .tsx files in `./viewers/` (it can; the test
            asserts the registry shape stays empty in this PR).

      AGENT.md update on `services/dashboard` referencing ADR-0070 and
      listing the new `src/review/` substrate.
    scope:
      files:
        - services/dashboard/src/review/types.ts
        - services/dashboard/src/review/useReviewKeyboard.ts
        - services/dashboard/src/review/ConfidenceStrip.tsx
        - services/dashboard/src/review/FlipThroughLayout.tsx
        - services/dashboard/src/review/registry.ts
        - services/dashboard/src/review/viewers/_README.txt
        - services/dashboard/src/review/useReviewKeyboard.test.tsx
        - services/dashboard/src/review/ConfidenceStrip.test.tsx
        - services/dashboard/src/review/FlipThroughLayout.test.tsx
        - services/dashboard/src/review/registry.test.ts
        - services/dashboard/AGENT.md
      services_affected:
        - services/dashboard
      out_of_scope:
        - services/dashboard/src/pages/TriageLabeling.tsx
        - services/dashboard/src/App.tsx
        - services/dashboard/src/api
        - services/dashboard/src/design
    validation:
      - kind: deterministic
        description: |
          Each new substrate source file is present.
          (node_modules is absent in the worker sandbox per
          docs/plans/2026-06-02-dashboard-triage-fixes-...md and the
          verify-binaries-exist-in-sandbox feedback; vitest + tsc run
          on the PR's CI workflow, not the in-worker gate.)
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/dashboard/src/review/types.ts" ]
          [ -f "$ROOT/services/dashboard/src/review/useReviewKeyboard.ts" ]
          [ -f "$ROOT/services/dashboard/src/review/ConfidenceStrip.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/FlipThroughLayout.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/registry.ts" ]
          [ -f "$ROOT/services/dashboard/src/review/useReviewKeyboard.test.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/ConfidenceStrip.test.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/FlipThroughLayout.test.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/registry.test.ts" ]
      - kind: deterministic
        description: |
          The substrate exposes the contracts ADR-0070 specifies:
          a viewer-props interface, a keyboard hook, an
          import.meta.glob registry, and the FlipThroughLayout
          wrapping PageLayout.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "ReviewKindViewerProps" "$ROOT/services/dashboard/src/review/types.ts"
          grep -q "ReviewLabelInput" "$ROOT/services/dashboard/src/review/types.ts"
          grep -q "useReviewKeyboard" "$ROOT/services/dashboard/src/review/useReviewKeyboard.ts"
          grep -q "import.meta.glob" "$ROOT/services/dashboard/src/review/registry.ts"
          grep -q "getViewer" "$ROOT/services/dashboard/src/review/registry.ts"
          grep -q "FlipThroughLayout" "$ROOT/services/dashboard/src/review/FlipThroughLayout.tsx"
          grep -q "PageLayout" "$ROOT/services/dashboard/src/review/FlipThroughLayout.tsx"
          grep -q "ConfidenceStrip" "$ROOT/services/dashboard/src/review/ConfidenceStrip.tsx"
      - kind: deterministic
        description: |
          The keyboard hook's input-focus guard is present and the
          test asserts the guard fires.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "activeElement" "$ROOT/services/dashboard/src/review/useReviewKeyboard.ts"
          grep -qE "INPUT|TEXTAREA|contenteditable" "$ROOT/services/dashboard/src/review/useReviewKeyboard.ts"
          grep -qE "input|INPUT|activeElement|focus" "$ROOT/services/dashboard/src/review/useReviewKeyboard.test.tsx"
      - kind: deterministic
        description: |
          AGENT.md references ADR-0070 and src/review (or the
          substrate's module names directly — accepts either
          a path-prefixed reference or a bare module reference).
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "ADR-0070" "$ROOT/services/dashboard/AGENT.md"
          grep -qE "src/review|FlipThroughLayout|useReviewKeyboard|ConfidenceStrip" "$ROOT/services/dashboard/AGENT.md"

  - id: review-route-wiring
    title: "ADR-0070 substep 1.4 — /review/:kind route + auto-discovery wire-up"
    workflow: wf-author
    depends_on:
      - task.review-queue-row-mixin.pr_merged
      - task.review-queue-router-factory.pr_merged
      - task.review-dashboard-chrome.pr_merged
    intent: |
      STUDY:
        - `services/dashboard/src/App.tsx` — the router has three
          routes today (`/`, `/tasks/:taskId`, `/triage`) plus a
          `*` wildcard that Navigates unknown paths to `/`. Add a
          fourth route `/review/:kind` mounted on a new `ReviewKind`
          page. The new Route MUST be added BEFORE the `*` wildcard
          Route or unknown paths under `/review/*` will be redirected
          to `/` instead of reaching `ReviewKind`'s in-page
          unknown-kind fallback panel.
        - `services/dashboard/src/review/registry.ts` (just landed)
          — the kind→component lookup the page uses.
        - `services/dashboard/src/api/queries.ts` — pattern for new
          query hooks. The page needs three hooks: `useReviewNext`,
          `useReviewStats`, `useLabelReviewRow`.

      BUILD `services/dashboard/src/pages/ReviewKind.tsx`:
        - `export function ReviewKind()` — reads `useParams<{ kind:
          string }>()`.
        - Calls `getViewer(kind)`; when null, renders a 404-style
          panel pointing at the registry contract.
        - Calls `useReviewNext(kind)`, `useReviewStats(kind)`, and
          `useLabelReviewRow(kind)`.
        - Maps the first unlabeled row + the LLM stats into the
          `FlipThroughLayout` props.
        - `onLabel` passes through to the mutation. The mutation's
          optimistic-update pattern mirrors `useLabelFinding` in
          `queries.ts` lines 145-182: drop the labeled row from the
          unlabeled cache so the next row materializes immediately.

      BUILD the three hooks in
      `services/dashboard/src/api/review_queries.ts`:
        - `useReviewNext(kind: string, *, limit?: number)`: GET
          `/api/v1/review/${kind}/next?limit=${limit ?? 20}`. Query
          key `['review', kind, 'next']`. staleTime 3000ms.
        - `useReviewStats(kind: string)`: GET `/api/v1/review/${kind}
          /stats`. Query key `['review', kind, 'stats']`. staleTime
          15000ms.
        - `useLabelReviewRow(kind: string)`: POST
          `/api/v1/review/${kind}/${id}/label` with the
          `ReviewLabelInput` body. Optimistic update drops the row
          from `['review', kind, 'next']`. Invalidates `['review',
          kind, 'stats']` on settle.

      WIRE the route in `services/dashboard/src/App.tsx`:
        - Import `ReviewKind` from `./pages/ReviewKind`.
        - Add `<Route path="/review/:kind" element={<ReviewKind />}
          />` between the existing `/triage` route and the fallback.

      Tests:
        - `services/dashboard/src/pages/ReviewKind.test.tsx`:
          * Render under MemoryRouter with `/review/unknown-kind`;
            assert the 404-style panel renders ("no viewer
            registered for unknown-kind" or similar) and that no
            network call is issued.
          * Render under MemoryRouter with `/review/_fake-kind` and
            register a tiny synthetic viewer via a `vi.mock(
            '../review/registry', ...)` that returns a stub viewer.
            Mock fetch with `vi.spyOn(global, 'fetch')` to return
            one row + a stats object. Assert the stub viewer's
            "rendered candidate" text appears.
          * Press 'space' on the page; assert fetch was called with
            POST `/api/v1/review/_fake-kind/{id}/label` carrying
            the LLM's label as the operator's verdict.
        - `services/dashboard/src/api/review_queries.test.tsx`:
          * `useReviewNext` issues GET to the correct URL.
          * `useLabelReviewRow` optimistic-update drops the labeled
            row from the cache (mirrors the triage label hook test
            shape in `services/dashboard/src/api/queries.test.tsx`).

      AGENT.md update on `services/dashboard` listing the new route +
      hooks file.
    scope:
      files:
        - services/dashboard/src/App.tsx
        - services/dashboard/src/pages/ReviewKind.tsx
        - services/dashboard/src/api/review_queries.ts
        - services/dashboard/src/pages/ReviewKind.test.tsx
        - services/dashboard/src/api/review_queries.test.tsx
        - services/dashboard/AGENT.md
      services_affected:
        - services/dashboard
      out_of_scope:
        - services/dashboard/src/pages/TriageLabeling.tsx
        - services/dashboard/src/api/queries.ts
        - services/dashboard/src/review/registry.ts
        - services/dashboard/src/review/FlipThroughLayout.tsx
        - services/api
    validation:
      - kind: deterministic
        description: |
          The new page + hooks + their test files all exist.
          (vitest + tsc run on PR CI, not in-worker — node_modules
          is absent in the worker sandbox.)
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/dashboard/src/pages/ReviewKind.tsx" ]
          [ -f "$ROOT/services/dashboard/src/api/review_queries.ts" ]
          [ -f "$ROOT/services/dashboard/src/pages/ReviewKind.test.tsx" ]
          [ -f "$ROOT/services/dashboard/src/api/review_queries.test.tsx" ]
      - kind: deterministic
        description: |
          ReviewKind page uses the substrate (FlipThroughLayout +
          registry lookup) and surfaces the unknown-kind fallback.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "FlipThroughLayout" "$ROOT/services/dashboard/src/pages/ReviewKind.tsx"
          grep -q "getViewer" "$ROOT/services/dashboard/src/pages/ReviewKind.tsx"
          grep -q "useParams" "$ROOT/services/dashboard/src/pages/ReviewKind.tsx"
          grep -q "useReviewNext\|useLabelReviewRow" "$ROOT/services/dashboard/src/pages/ReviewKind.tsx"
      - kind: deterministic
        description: |
          review_queries.ts exposes the three required hooks with
          query-key + path patterns that match ADR-0070's endpoints.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "useReviewNext" "$ROOT/services/dashboard/src/api/review_queries.ts"
          grep -q "useReviewStats" "$ROOT/services/dashboard/src/api/review_queries.ts"
          grep -q "useLabelReviewRow" "$ROOT/services/dashboard/src/api/review_queries.ts"
          grep -q "/api/v1/review" "$ROOT/services/dashboard/src/api/review_queries.ts"
          grep -q "'next'\|/next" "$ROOT/services/dashboard/src/api/review_queries.ts"
          grep -q "'stats'\|/stats" "$ROOT/services/dashboard/src/api/review_queries.ts"
      - kind: deterministic
        description: |
          App.tsx mounts the new route.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "/review/:kind" "$ROOT/services/dashboard/src/App.tsx"
          grep -q "ReviewKind" "$ROOT/services/dashboard/src/App.tsx"
      - kind: deterministic
        description: |
          AGENT.md references the new route + hooks file. Accepts
          all common path-param notations (FastAPI `{kind}`,
          react-router `:kind`, angle-bracket `<kind>`, generic
          `[kind]`).
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -qE "/review/[:<{[]kind" "$ROOT/services/dashboard/AGENT.md"
          grep -q "review_queries" "$ROOT/services/dashboard/AGENT.md"
```

## Diagram

Not applicable. ADR-0070's per-kind data flow + cybernetic-loop
sequence cover the architecture; this plan just lays the substrate
those flows ride on.

## Risks / unknowns

- **Mixin verdict-column delegation.** Each kind must supply its own
  `llm_label` + operator-verdict column because each kind has its own
  enum. The mixin documents this contract but cannot enforce it at
  declaration time without metaclass tricks. Mitigation: the synthetic
  `_FakeKindRow` test in Task 1 + the synthetic-kind factory tests in
  Task 2 catch the omission at first-use; substep 2 (triage refactor)
  re-validates against a real production-shaped subclass.
- **`compute_stats` raw-SQL vs ORM.** The stats SQL is mildly tricky
  (the `accuracy_last_100` requires a subquery). The plan calls for
  ORM-only; if the worker hits a SQLAlchemy expression-typing wall,
  the fallback is `session.execute(text(...))` with parameter binding
  on `row_cls.__tablename__`. Documented here so the worker can pivot
  without architect intervention.
- **`useReviewKeyboard` input guard.** The keydown listener attaches
  to `window`; if a kind's viewer mounts a CodeMirror-style editor
  that intercepts keydowns at the capture phase, our handler still
  fires. Mitigation: the `enabled` prop on the hook + the
  `document.activeElement` guard cover the common case (`<input>`,
  `<textarea>`, `[contenteditable]`); v1 accepts that exotic editors
  must opt out via `enabled={false}` on the layout.
- **`import.meta.glob` test ergonomics.** Vitest evaluates the glob
  at module load; if the empty `viewers/` directory means zero
  matches, the registry's empty-state needs to be the v1 default.
  Task 3's `registry.test.ts` pins this.
- **Substep-2 coupling on the mixin shape.** If substep 2 (triage
  refactor) discovers a missing field on the mixin, this plan's
  substrate has to be amended before the kinds plan can land. We
  accept the iteration cost; the alternative (designing the mixin
  off a single precedent) is worse.
- **No new alembic migration this plan.** Intentional — substep 2
  refactors triage without changing its table, substep 3 brings the
  first new kind's migration. If the worker accidentally creates an
  empty migration, plan-validate doesn't catch it; the architect
  should reject.

## Decisions captured during execution

(empty at draft time)

## Post-mortem

(filled when plan transitions to completed)
