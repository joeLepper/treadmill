---
auto_merge: true
status: active
---

# Plan: ADR-0070 substep 2 — refactor ADR-0061 triage onto the review-queue framework

- **Status:** active
- **Date:** 2026-06-04
- **Related ADRs:** ADR-0070 (the design), ADR-0061 (the triage surface
  being migrated), ADR-0056 (the auto-discovery seam the new endpoints +
  viewers ride on), ADR-0052 (the corpus shape downstream)

## Goal

Adopt the ADR-0070 review-queue framework on the already-working
triage-finding surface. The `triage_findings` schema does not move —
`TriageFindingRow` already conforms to the six-layer shape ADR-0070
generalizes. What moves is the *labeling surface itself*: the bespoke
`routers/triage/labels.py` endpoints + `pages/TriageLabeling.tsx` page
get refactored to consume the framework substrate landed in substep 1
(`ReviewQueueRowMixin`, the shared dashboard chrome, the per-kind viewer
registry, the `/api/v1/review/<kind>` mount pattern). When this substep
closes, the operator-visible URL is `/review/triage-finding`, the
endpoints live under `/api/v1/review/triage-finding/`, and adding a new
kind in substep 3 is a "drop two files + a migration" exercise.

Proving the abstraction on an already-working surface — with real
fixtures, real labels, real operator traffic — is the contract this
substep ships. If the framework cannot host triage, we learn it here
before authoring `architect-gold` / `validator-gold` against the same
substrate.

## Success criteria

1. A new auto-discovered router module
   `services/api/treadmill_api/routers/review/review_triage_finding.py`
   exists, calling `build_review_router(...)` from substep 1's
   `routers/review/base.py` (do NOT hand-roll the endpoints). The
   resulting router exposes `GET /next?limit=N`, `GET /{id}`,
   `POST /{id}/label`, and `GET /stats` mounted under
   `/api/v1/review/triage-finding`. The endpoints read and write the
   same `triage_findings` table that ADR-0061 ships; the row schema
   does not move. The factory call binds the existing `confidence`
   column to the framework's `llm_confidence` contract via the factory
   signature (the row already carries `confidence`; no rename, no
   migration). The `verdict_attr="label_is_real_bug"` argument and a
   SQL-expression `llm_label_attr` aliasing `confidence != 'low'`
   are passed to satisfy the v1 accuracy proxy.
2. The `/next` endpoint orders by `(confidence ASC via a CASE
   expression that maps 'low'→0, 'medium'→1, 'high'→2, then
   created_at ASC)` per ADR-0070's "least-confident first" contract.
   The `/stats` endpoint returns `{total, unlabeled, labeled_total,
   label_accuracy, accuracy_last_100}` from substep 1's
   `compute_stats` helper, where `label_accuracy` is the fraction of
   rows where `label_is_real_bug IS NOT NULL` AND
   `label_is_real_bug = (confidence != 'low')` — i.e. low-confidence
   rows count as a 'false' LLM proposal and ARE included in the
   denominator. (The accuracy math is documented in the module
   docstring; v1 closes the loop on what the existing schema can
   answer, and a TODO marker pins the v2 enum-mapping work for when
   richer LLM-label columns land.)
3. A new viewer `services/dashboard/src/review/viewers/triage-finding.tsx`
   exists (the substep-1 registry uses
   `import.meta.glob('./viewers/*.tsx')`, so the file lands under
   `viewers/`) and is auto-registered into the framework's
   kind-to-component map. It conforms to the substep-1
   `ReviewKindViewer` contract — `(props: ReviewKindViewerProps<...>)
   => React.ReactElement` with `{row, onLabel}` props where
   `row: ReviewRow<TriageCandidate, ...>` carries
   `{id, created_at, source_url, source_pr_number, candidate, llm}`
   and `onLabel: (input: ReviewLabelInput) => void`. The viewer
   renders the existing triage-evidence layout (screenshot,
   observation, evidence pointer, proposed resolution). Page chrome
   (sidebar, top bar, keyboard shortcuts strip, accuracy widget)
   comes from the framework's `<FlipThroughLayout>`; the viewer
   renders the candidate body only.
4. The legacy `/triage` route in `App.tsx` redirects to
   `/review/triage-finding`. The old `pages/TriageLabeling.tsx` module
   is deleted in the same PR that adds the viewer (no parallel surfaces).
5. The legacy `routers/triage/labels.py` GET / POST endpoints remain
   wired (the framework re-implements them under `/api/v1/review/...`,
   but the old surface stays live for one release so any cached
   bookmarks / role-ui-triage scripts pointing at `/api/v1/triage/...`
   don't 404). Removal is pinned to substep 4 in the new module's
   docstring + the AGENT.md deprecation pointer (Task A intent). This
   substep does NOT edit `routers/triage/labels.py` itself.
6. Existing labeling integration tests
   (`test_routers_triage_labels.py`, `test_triage_store.py`) continue
   to pass unchanged. New tests cover the framework-mounted endpoints
   end-to-end: `GET /next` ordering, `GET /stats` math (including the
   `label_accuracy = NaN` empty-corpus edge), `POST /label` round-trip,
   `GET /{id}` 404, and the auto-discovery contract (a synthetic
   sibling under `routers/dashboard/` is mounted by the same pass).
7. `services/api/AGENT.md` and `services/dashboard/AGENT.md` gain
   ADR-0070 references describing the new mount + viewer-registry
   contract, plus the legacy-endpoint deprecation note.

## Constraints / scope

### In scope

- New auto-discovered router under `routers/dashboard/`
  (`review_triage_finding.py`) implementing the per-kind contract from
  ADR-0070's "Auto-discovered routers" section.
- New viewer module at `services/dashboard/src/review/triage-finding.tsx`
  registered into the framework's kind-to-component map.
- Route rewiring in `services/dashboard/src/App.tsx` — `/triage`
  redirects, `/review/:kind` lands on the framework page.
- Delete `services/dashboard/src/pages/TriageLabeling.tsx` (its
  responsibilities split between the viewer and the framework chrome
  shipped in substep 1).
- Stats math against existing columns: `total` = count(rows),
  `unlabeled` = count(`label_is_real_bug IS NULL`), `labeled_total` =
  count(`label_is_real_bug IS NOT NULL`), `label_accuracy` = fraction
  of labeled rows where `label_is_real_bug = (confidence != 'low')`,
  `accuracy_last_100` = same over the most recent 100 labeled rows.
- AGENT.md updates on `services/api/` + `services/dashboard/`.

### Out of scope

- Schema migrations on `triage_findings`. The row already conforms;
  no new columns, no constraint changes, no migration in this substep.
- Adding `llm_label` / `llm_confidence` / `llm_rationale` columns
  beyond what `TriageFindingRow` already carries (`confidence`,
  `observation`, `proposed_resolution`, `prompt_version`, `model`).
  Those are aliased into the viewer's `llm_recommendation` payload at
  the router boundary; the table is not migrated. The richer per-kind
  LLM columns land in substep 3 when new kinds are introduced.
- Deleting the legacy `routers/triage/labels.py` + `findings.py`
  endpoints. One-release deprecation; removal pinned to substep 4.
- Other review-queue kinds (`architect-gold`, `validator-gold`,
  `dspy-variant-pr`, etc.) — substep 3 onward.
- Operator-keyboard customization beyond what the framework's shared
  chrome provides. The framework's keybindings (`space` = accept,
  `x` = reject, `s` = skip, `j`/`k` = nav, `?` = guidelines) are taken
  as-is.
- The `role-ui-triage` proposing role itself. ADR-0061 already ships
  it; no changes to its prompt, its schedule, or its insert path.

### Budget

Three worker dispatches, sequenced (Task A → Task B → Task C). If
either of the first two wedges at the architect cap, do not dispatch
the next — investigate first. Each task is one PR; auto_merge: true
applies at the plan level and each task inherits it.

## Sequence of work

```yaml
sequence_of_work:
  - id: review-triage-finding-router
    title: "ADR-0070 substep 2 step 1 — mount triage-finding under /api/v1/review/ via build_review_router"
    workflow: wf-author
    intent: |
      STUDY:
        - `services/api/treadmill_api/routers/review/__init__.py` and
          `services/api/treadmill_api/routers/review/base.py` (LANDED
          BY SUBSTEP 1). The package + `build_review_router(...)`
          factory + `StatsResponse` already exist. This task does NOT
          re-author either file. The new per-kind module is dropped
          alongside as a sibling; the auto-discovery pass in
          `routers/review/__init__.py` mounts it under `/api/v1/review`
          with no `__init__` edit.
        - `services/api/treadmill_api/services/review_stats.py` (LANDED
          BY SUBSTEP 1). `compute_stats(session, *, row_cls,
          verdict_attr, llm_label_attr)` is the single source of truth
          for the stats math — this task wires `TriageFindingRow` into
          it, it does NOT re-implement count / accuracy SQL.
        - `services/api/treadmill_api/routers/triage/labels.py` —
          the legacy labeling endpoints (`GET /findings`,
          `POST /findings/{id}/label`). This module STAYS in place
          this substep — do not delete, move, or edit it. Read the
          `LabelFindingRequest` shape: this task either re-uses it
          directly OR (preferred) declares a sibling
          `LabelFindingRequest`-shaped input model on the new module
          with `model_config = ConfigDict(extra="allow")` so the
          framework's mutation can pass through kind-specific extras
          (`label_severity`, `label_category`, `label_fix_in_dsl`).
          Document the chosen shape explicitly in the module docstring.
        - `services/api/treadmill_api/triage_store.py` — for the
          existing async-session pattern (read-only). The factory
          owns the queries; no new TriageStore method needed in
          this task.
        - `services/api/treadmill_api/models/triage_finding.py` —
          `TriageFindingRow`. The existing detector column is named
          `confidence` (NOT `llm_confidence`). The v1 accuracy math
          must reference the column named `confidence`. Document this
          alias-by-name in the new module's docstring + leave a TODO
          marker that pins the v2 enum-mapping work to substep 3
          (when richer LLM-label columns land alongside new kinds).
        - `docs/adrs/0070-pre-labeled-review-queues.md` — the
          "Auto-discovered routers" + "Per-kind table shape" sections.

      BUILD a new sibling module
      `services/api/treadmill_api/routers/review/review_triage_finding.py`
      that declares a module-level `router = build_review_router(...)`
      attribute so substep 1's `pkgutil.iter_modules` auto-discovery
      mounts it. The factory call passes:
        - `prefix="/triage-finding"` (the aggregator owns
          `/api/v1/review`, so the effective mount is
          `/api/v1/review/triage-finding/...`).
        - `row_cls=TriageFindingRow`.
        - `verdict_attr="label_is_real_bug"` (the operator-verdict
          column on the existing ADR-0061 schema).
        - `llm_label_attr` is the framework's accuracy-math name; the
          existing schema has no `llm_label` column. Pass a
          SQL-expression alias that resolves to `confidence != 'low'`
          cast to boolean. Two acceptable shapes (pick one and
          document in the module docstring):
            (a) Extend `build_review_router`'s signature in a tiny
                follow-up so `llm_label_attr` may be a SQLAlchemy
                column-expression callable, then pass
                `lambda: case((TriageFindingRow.confidence != 'low', True), else_=False)`.
                This is the cleanest answer if substep 1 ships before
                this task.
            (b) Declare a `@hybrid_property` `llm_label` on a thin
                subclass / mixin overlay used only by the factory call
                and pass `llm_label_attr="llm_label"`. Simpler but
                couples the alias to the row class.
          The plan accepts either; the chosen path is documented in
          the module docstring and in Task A's PR description.
        - `label_input_model=LabelFindingRequest` (per
          `routers/triage/labels.py`) OR a sibling-declared model that
          accepts the same five `label_*` fields plus `labeled_by`
          with `model_config = ConfigDict(extra="allow")` so the
          framework's `{label, override_reason?, notes?, extras?}`
          adapter (Task B) lands cleanly. Pick one and document.
        - `output_model=TriageFinding` (from
          `treadmill_api.schemas.triage_finding`).

      The factory mounts the four mandatory endpoints (`GET /next`,
      `GET /{id}`, `POST /{id}/label`, `GET /stats`); ordering on
      `/next` is the factory's CASE-mapped
      `low → 0, medium → 1, high → 2 then created_at ASC` (substep 1
      contract). This task adds NO endpoints of its own.

      Tests in `services/api/tests/test_routers_review_triage_finding.py`
      (new file; mirror `test_routers_triage_labels.py`'s hermetic-
      stub style — see "stub session" pattern, NOT the integration
      pattern in `test_triage_store.py` which is gated on
      `TREADMILL_INTEGRATION=1` and silently skips in the worker
      sandbox):
        - `GET /api/v1/review/triage-finding/next` returns ordered
          rows least-confident first. Fixture with five rows
          (confidence=high old, high newer, medium, low, low newer
          — interleaved created_at so lexical ordering produces a
          different result than CASE ordering) all unlabeled; assert
          response order is `low → low_newer → medium → high → high_newer`.
          This distinguishes correct CASE ordering from raw string
          ordering even if a future enum changes.
        - `GET /api/v1/review/triage-finding/{id}` happy + 404 paths.
        - `POST /api/v1/review/triage-finding/{id}/label` round-trip
          — write a label (`label_is_real_bug=true`,
          `labeled_by='op'`), re-GET via `/next`, assert the labeled
          row is no longer in the queue.
        - `POST .../label` with `label_is_real_bug=null` (the
          ADR-0061 Skip semantic — null is a signal) — assert the
          row is persisted with `labeled_at` stamped and the row
          drops out of `/next` (the `unlabeled` query filters by
          `label_is_real_bug IS NULL`, so a Skip-labeled row would
          re-appear unless the unlabeled predicate is changed; this
          test pins the v1 behavior as observed and the docstring
          notes the v2 question).
        - `POST .../label` with the kind-specific extras
          (`label_severity`, `label_category`, `label_fix_in_dsl`)
          included — assert they round-trip into the row's existing
          columns. This pins the chosen input-shape decision above.
        - `GET /api/v1/review/triage-finding/stats` against a
          fixture with 5 labeled rows where 4 match the
          `(confidence != 'low')` alias and 1 mismatches → returns
          `{total: 5, unlabeled: 0, labeled_total: 5,
            label_accuracy: 0.8, accuracy_last_100: 0.8}`.
        - `GET /stats` against a fixture with 102 labeled rows where
          the oldest 2 disagree with the alias and the newest 100 all
          match → returns `label_accuracy ≈ 100/102` AND
          `accuracy_last_100 = 1.0`. This proves the
          `LIMIT 100` subquery actually clips.
        - `GET /stats` against an empty corpus → returns
          `{total: 0, unlabeled: 0, labeled_total: 0,
            label_accuracy: null, accuracy_last_100: null}`.

      AGENT.md update on `services/api/AGENT.md`: a new bullet under
      the existing "Recent changes" / latest-PR section describing
      the `routers/review/review_triage_finding.py` sibling module,
      the call to substep 1's `build_review_router` factory, the
      four mandatory endpoints mounted at
      `/api/v1/review/triage-finding/`, the v1 stats-aliasing
      (`confidence != 'low'` aliases the framework's `llm_label`
      contract) + the v2 TODO, and a deprecation pointer to substep
      4 for the legacy `routers/triage/` removal. The bullet MUST
      mention 'viewer registry', '/api/v1/review/triage-finding',
      and 'substep 4'.

      Do NOT touch the legacy `routers/triage/labels.py` or
      `findings.py` endpoints. Both continue to serve the same
      response shapes. The new module is additive; deprecation
      happens in substep 4.

      Do NOT modify the `triage_findings` schema. No Alembic
      migration in this task.

      Do NOT re-author `routers/review/__init__.py`,
      `routers/review/base.py`, `services/review_stats.py`, or the
      `app.py` review_router include — those land in substep 1. This
      task drops a sibling module that the existing auto-discovery
      picks up.

      PRE-FLIGHT (the worker MUST check before any edit; substep 1 is
      a separate plan whose tasks cannot be expressed in this plan's
      depends_on graph):
        - Confirm `services/api/treadmill_api/routers/review/base.py`
          exists and exports `build_review_router`.
        - Confirm `services/api/treadmill_api/routers/review/__init__.py`
          exists and is the aggregator mounted at `/api/v1/review`.
        - Confirm `services/api/treadmill_api/services/review_stats.py`
          exists and exports `compute_stats`.
        If any of the three is missing, STOP and surface the gap —
        do NOT re-author the substrate inside this task.
    scope:
      files:
        - services/api/treadmill_api/routers/review/review_triage_finding.py
        - services/api/tests/test_routers_review_triage_finding.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/routers/review/__init__.py
        - services/api/treadmill_api/routers/review/base.py
        - services/api/treadmill_api/services/review_stats.py
        - services/api/treadmill_api/app.py
        - services/api/treadmill_api/models/triage_finding.py
        - services/api/treadmill_api/triage_store.py
        - services/api/tests/test_triage_store.py
        - services/api/treadmill_api/routers/triage/labels.py
        - services/api/treadmill_api/routers/triage/findings.py
        - services/api/treadmill_api/schemas/triage_finding.py
        - services/dashboard/
    validation:
      - kind: deterministic
        description: |
          The new review-router tests pass against the
          framework-mounted endpoints, including ordering, stats math
          (including the 102-row LIMIT 100 boundary case), and the
          extras-passthrough on POST /label.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          cd "$ROOT/services/api" && uv run pytest tests/test_routers_review_triage_finding.py -q
      - kind: deterministic
        description: |
          The legacy labeling tests still pass — the new module is
          additive and does not regress the existing surface.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          cd "$ROOT/services/api" && uv run pytest tests/test_routers_triage_labels.py tests/test_routers_triage_findings.py -q
      - kind: deterministic
        description: |
          The new sibling module declares a top-level APIRouter
          (returned by build_review_router) so the substep-1
          auto-discovery pass mounts it.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/treadmill_api/routers/review/review_triage_finding.py" ]
          grep -qE "build_review_router" "$ROOT/services/api/treadmill_api/routers/review/review_triage_finding.py"
          grep -qE "^router[^=]*=" "$ROOT/services/api/treadmill_api/routers/review/review_triage_finding.py"
      - kind: deterministic
        description: |
          The factory call binds the triage-finding row + verdict
          attr per ADR-0070, and references the existing 'confidence'
          column (NOT a non-existent 'llm_confidence' column).
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          FILE="$ROOT/services/api/treadmill_api/routers/review/review_triage_finding.py"
          grep -qE "TriageFindingRow" "$FILE"
          grep -qE 'verdict_attr\s*=\s*["'\'']label_is_real_bug' "$FILE"
          grep -qE 'prefix\s*=\s*["'\'']/triage-finding' "$FILE"
          grep -qE "confidence" "$FILE"
      - kind: deterministic
        description: |
          The mounted FastAPI app exposes the four mandatory ADR-0070
          endpoints under /api/v1/review/triage-finding/. This proves
          the sibling was picked up by the substep-1 auto-discovery,
          not just declared.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          cd "$ROOT/services/api" && uv run python -c "
          from treadmill_api.app import create_app
          app = create_app()
          paths = {r.path for r in app.routes if hasattr(r, 'path')}
          required = {
              '/api/v1/review/triage-finding/next',
              '/api/v1/review/triage-finding/stats',
              '/api/v1/review/triage-finding/{finding_id}',
              '/api/v1/review/triage-finding/{finding_id}/label',
          }
          missing = required - paths
          assert not missing, f'missing: {missing}; have: {sorted(p for p in paths if \"review\" in p)}'
          "
      - kind: deterministic
        description: |
          AGENT.md references ADR-0070 plus the specific contract
          phrases the success criteria mandate.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -qE "ADR-0070" "$ROOT/services/api/AGENT.md"
          grep -qE "/api/v1/review/triage-finding" "$ROOT/services/api/AGENT.md"
          grep -qE "substep 4|deprecat" "$ROOT/services/api/AGENT.md"

  - id: review-triage-finding-viewer
    title: "ADR-0070 substep 2 step 2 — register triage-finding viewer + delete legacy page"
    workflow: wf-author
    depends_on:
      - task.review-triage-finding-router.pr_merged
    intent: |
      PRE-FLIGHT (the worker MUST confirm before any edit; substep 1
      is a separate plan):
        - `services/dashboard/src/review/FlipThroughLayout.tsx`,
          `services/dashboard/src/review/registry.ts`, and
          `services/dashboard/src/review/types.ts` all exist.
        - `services/dashboard/src/pages/ReviewKind.tsx` exists.
        - `services/dashboard/src/App.tsx` already mounts
          `<Route path="/review/:kind" element={<ReviewKind />} />`.
        If any are missing, STOP — do NOT add the route here; that
        is substep 1's responsibility.


      STUDY:
        - `services/dashboard/src/App.tsx` — the route map. Substep 1
          (`review-route-wiring`) already adds
          `<Route path="/review/:kind" element={<ReviewKind />} />`.
          This task does NOT add that route. It DOES change the
          existing `/triage` route to a Navigate-redirect to
          `/review/triage-finding`.
        - `services/dashboard/src/pages/TriageLabeling.tsx` — the
          existing flip-through labeling page. The
          `EvidenceColumn` / `LabelColumn` decomposition is preserved
          but moves into a viewer component. The PageLayout wrapping
          + draft-state management + onSubmit hook → mutation wiring
          belongs to the framework chrome (landed in substep 1), NOT
          the viewer.
        - `services/dashboard/src/api/queries.ts` lines 130-182 —
          `useUnlabeledFindings` + `useLabelFinding`. These hooks
          become DEAD CODE the moment `pages/TriageLabeling.tsx` is
          deleted (the only caller). The plan-level posture is "do
          not delete them yet, substep 4 removes them" — add a
          one-line `// TODO(substep 4): remove with the legacy
          /api/v1/triage/ endpoints` comment ABOVE each export so a
          future worker doesn't 'helpfully' delete them and so the
          dashboard build's unused-export analyzer (if any) treats
          this as intentional.
        - `services/dashboard/src/api/types.ts` — `TriageFinding`,
          `TriageLabelInput`, the enum types. No changes needed; the
          viewer imports them as-is.
        - The substep-1-landed dashboard chrome:
            * `services/dashboard/src/review/FlipThroughLayout.tsx`
              — the chrome the framework page mounts. Viewer slot
              is invoked with `{row, onLabel}` props per substep 1's
              `ReviewKindViewerProps<TCandidate, TLlm>`.
            * `services/dashboard/src/review/registry.ts` —
              `import.meta.glob('./viewers/*.tsx', { eager: true })`.
              The new viewer's file MUST land at
              `services/dashboard/src/review/viewers/triage-finding.tsx`
              for the registry to pick it up.
            * `services/dashboard/src/review/types.ts` —
              `ReviewKindViewer`, `ReviewKindViewerProps`,
              `ReviewRow`, `ReviewLabelInput`.
            * `services/dashboard/src/pages/ReviewKind.tsx` (substep
              1) — the framework page that wires
              `useReviewNext('triage-finding')` →
              `useLabelReviewRow('triage-finding')` →
              `FlipThroughLayout`. This task does NOT edit it.
        - ADR-0070 "Auto-discovered viewers" + "Contract for a viewer"
          sections — the props contract this task adopts.

      BUILD a new viewer
      `services/dashboard/src/review/viewers/triage-finding.tsx` with
      a default export
      `TriageFindingViewer({row, onLabel}: ReviewKindViewerProps<TriageCandidate, string>)`
      that:
        - Lays out the existing `EvidenceColumn` content (screenshot,
          observation, evidence_pointer, proposed_resolution) in the
          left/main region using `row.candidate`. Move the helpers
          (`FieldBlock`, `FieldLabel`, `Mono`, `Paragraph`,
          `Screenshot`, `Header`) into this module verbatim from
          `TriageLabeling.tsx`.
        - Renders `row.llm` (the substep-1 `ReviewLlmRecommendation`
          shape: `{label, confidence, rationale, prompt_version,
          model}`) as a labeled card showing `confidence`,
          `rationale`, and the detector's observation excerpt /
          proposed_resolution from `row.candidate`. The framework's
          shared chrome owns the accept/reject buttons; this viewer
          renders the LLM-side context only.
        - The label-collection sidebar (the existing `LabelColumn`
          with `is_real_bug` / `severity` / `category` /
          `fix_in_dsl` / `notes` controls) becomes a viewer-owned
          slot: the framework passes an `onLabel: (input:
          ReviewLabelInput) => void` callback. The viewer adapts the
          tristate selectors → ReviewLabelInput by flattening the
          extras into top-level keys before calling onLabel, e.g.:
            `onLabel({
               label: String(label_is_real_bug),  // 'true'/'false'/'null' or chosen string
               override_reason: label_notes ?? undefined,
               notes: label_notes ?? undefined,
               labeled_by: 'operator',
               // kind-specific extras (Task A's input model accepts
               // them via ConfigDict(extra="allow") or by declaring
               // them as siblings):
               label_severity, label_category, label_fix_in_dsl,
            } as ReviewLabelInput & {...})`.
          (The exact serialization depends on Task A's chosen input
          shape — read its module docstring first and match.) The
          framework's mutation hook forwards the body to
          `POST /api/v1/review/triage-finding/{id}/label`.
        - Imports `TriageFinding` from `../../api/types` (note: the
          file moved one level deeper into `viewers/`).
        - Submit-path coverage MUST include three cases: accept
          (`is_real_bug=true`), reject (`is_real_bug=false`), and
          skip (`is_real_bug=null`) — ADR-0061 treats null as a
          signal, the test pins the contract.

      EDIT `services/dashboard/src/App.tsx`:
        - Change the existing `<Route path="/triage" ...>` to a
          redirect: `<Route path="/triage" element={<Navigate
          to="/review/triage-finding" replace />} />`. This preserves
          existing bookmarks.
        - Remove the `import { TriageLabeling }` line.
        - Do NOT add the `/review/:kind` route — substep 1's
          `review-route-wiring` task already adds it. If it is not
          present (substep 1 has not yet merged), STOP and surface
          the gap; do not duplicate the route.

      DELETE `services/dashboard/src/pages/TriageLabeling.tsx` and its
      sibling test if one exists. The viewer + framework page replace
      it. (Check
      `services/dashboard/src/pages/TriageLabeling.test.tsx` first —
      delete if present, port any unique assertions into a new
      `services/dashboard/src/review/viewers/triage-finding.test.tsx`.)

      NEW test
      `services/dashboard/src/review/viewers/triage-finding.test.tsx`
      (using vitest + react-testing-library — mirror
      `pages/TaskDetail.test.tsx`'s style — this test runs on the PR's
      CI vitest job, NOT in the worker sandbox which has no
      dashboard node_modules):
        - Renders the viewer with a fixture `row` whose `candidate`
          carries screenshot / observation / evidence_pointer / etc.
          — asserts all the on-screen fields appear.
        - Renders the LLM card from `row.llm` — asserts confidence +
          rationale appear.
        - Accept path: supply a `vi.fn()` as `onLabel`, click the
          submit button after toggling `is_real_bug = Yes`, assert
          `onLabel` was called once with `label='true'` (or whatever
          serialization Task A's input model expects).
        - Reject path: same with `is_real_bug = No` → assert
          `label='false'`.
        - Skip path: submit with all label fields null → assert
          `onLabel` was called with the Skip-shape (null verdict);
          mirrors `test_routers_triage_labels.py`'s
          `test_post_label_with_all_null_labels_is_accepted` coverage.

      NEW App-route redirect test
      `services/dashboard/src/App.test.tsx` (or extend an existing
      App test if one lands later): mount `<App />` inside
      `<MemoryRouter initialEntries={['/triage']}>`, assert the
      framework page chrome (e.g. the `FlipThroughLayout` title or a
      registry-resolved viewer marker) renders and the legacy
      `TriageLabeling` heading does NOT. This asserts the Navigate
      actually fires, not just that the literal `path="/triage"`
      string is in App.tsx.

      AGENT.md update on `services/dashboard/AGENT.md`: bullet
      describing the new `src/review/viewers/` directory, the
      viewer-registry contract (default export, `{row, onLabel}`
      props per the substep-1 `ReviewKindViewerProps` type), the
      route rewiring (`/triage` → `/review/triage-finding`), and the
      deprecation pointer for the legacy `pages/TriageLabeling.tsx`
      / `/api/v1/triage/` endpoints. The bullet MUST mention
      'viewer registry', 'kind-to-component', and 'substep 4'.

      Do NOT touch the API code — the new endpoints landed in Task A.
      Do NOT delete the legacy `routers/triage/labels.py` endpoints
      or the `useUnlabeledFindings` / `useLabelFinding` query hooks;
      those go in substep 4. Annotate them with a TODO comment
      instead.

      Do NOT add new design-system components. Reuse `Button`,
      `StateBadge`, the existing tokens. The framework's
      `<FlipThroughLayout>` provides sidebar / topbar / accuracy
      widget; the viewer's job is candidate body + LLM card + label
      sidebar only.
    scope:
      files:
        - services/dashboard/src/App.tsx
        - services/dashboard/src/App.test.tsx
        - services/dashboard/src/review/viewers/triage-finding.tsx
        - services/dashboard/src/review/viewers/triage-finding.test.tsx
        - services/dashboard/src/pages/TriageLabeling.tsx
        - services/dashboard/src/pages/TriageLabeling.test.tsx
        - services/dashboard/src/api/queries.ts
        - services/dashboard/AGENT.md
      services_affected:
        - services/dashboard
      out_of_scope:
        - services/dashboard/src/review/FlipThroughLayout.tsx
        - services/dashboard/src/review/registry.ts
        - services/dashboard/src/review/types.ts
        - services/dashboard/src/pages/ReviewKind.tsx
        - services/dashboard/src/api/review_queries.ts
        - services/dashboard/src/api/types.ts
        - services/dashboard/src/design/
        - services/api/
    validation:
      # node_modules is absent from the worker sandbox (workers/agent/
      # Dockerfile only npm-installs claude-code, aws-cdk, playwright
      # — not the dashboard's vitest/react-testing-library devDeps).
      # The substep-1 dashboard tasks follow the same pattern: file-
      # existence + grep gates only; vitest runs on the PR's CI job.
      # See docs/plans/2026-06-04-adr-0070-substep-1-framework-substrate.md
      # validation blocks for the established shape.
      - kind: deterministic
        description: |
          The legacy page is gone, the viewer exists at the
          registry-discovered path, and App.tsx wires the redirect.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ ! -f "$ROOT/services/dashboard/src/pages/TriageLabeling.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/viewers/triage-finding.tsx" ]
          [ -f "$ROOT/services/dashboard/src/review/viewers/triage-finding.test.tsx" ]
          [ -f "$ROOT/services/dashboard/src/App.test.tsx" ]
          grep -q "/review/triage-finding" "$ROOT/services/dashboard/src/App.tsx"
          grep -q "Navigate" "$ROOT/services/dashboard/src/App.tsx"
          grep -qE '/triage["'\'']' "$ROOT/services/dashboard/src/App.tsx"
      - kind: deterministic
        description: |
          The viewer default-exports a component and references all
          three substep-1 contract identifiers (row, onLabel, and the
          framework type). Three separate greps — not an alternation
          — so the gate fails if any one is missing.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          FILE="$ROOT/services/dashboard/src/review/viewers/triage-finding.tsx"
          grep -q "export default" "$FILE"
          grep -q "row" "$FILE"
          grep -q "onLabel" "$FILE"
          grep -qE "ReviewKindViewer|ReviewKindViewerProps" "$FILE"
      - kind: deterministic
        description: |
          The legacy queries.ts hooks carry a substep-4 TODO so they
          survive future dead-code passes and don't get
          'helpfully' removed before substep 4.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          FILE="$ROOT/services/dashboard/src/api/queries.ts"
          grep -qE "substep 4" "$FILE"
          grep -qE "useUnlabeledFindings|useLabelFinding" "$FILE"
      - kind: deterministic
        description: |
          AGENT.md references ADR-0070 plus the specific contract
          phrases the success criteria mandate.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -qE "ADR-0070" "$ROOT/services/dashboard/AGENT.md"
          grep -qE "viewer registry|kind-to-component" "$ROOT/services/dashboard/AGENT.md"
          grep -qE "substep 4|deprecat" "$ROOT/services/dashboard/AGENT.md"

  - id: review-triage-finding-integration-smoke
    title: "ADR-0070 substep 2 step 3 — wire end-to-end + verify accuracy widget reads /stats"
    workflow: wf-author
    depends_on:
      - task.review-triage-finding-router.pr_merged
      - task.review-triage-finding-viewer.pr_merged
    intent: |
      STUDY:
        - Task A's new
          `services/api/treadmill_api/routers/review/review_triage_finding.py`
          and its `build_review_router(...)` call — confirm the
          factory parameters and the `confidence`-as-llm-label alias.
        - Task B's new
          `services/dashboard/src/review/viewers/triage-finding.tsx`
          — confirm the viewer's props match the framework contract
          (`ReviewKindViewerProps<TriageCandidate, string>`).
        - Substep 1's
          `services/dashboard/src/pages/ReviewKind.tsx` — the
          framework page. It calls `useReviewStats(kind)` (from
          `services/dashboard/src/api/review_queries.ts`) and
          drives the `ConfidenceStrip` accuracy widget. This task
          does NOT change either of those files. It only verifies
          they read `/api/v1/review/triage-finding/stats` correctly
          end-to-end with a kind-aware fetch mock.

      BUILD an integration test that exercises the full path against
      the existing FastAPI test client + the stub-session pattern
      from `test_routers_triage_labels.py` (NOT the integration-
      gated pattern in `test_triage_store.py` which is gated on
      `TREADMILL_INTEGRATION=1` and silently skips in the worker
      sandbox):
        - File: `services/api/tests/test_review_triage_finding_loop.py`.
        - Seed 5 fixture rows with EXPLICIT, deterministic
          `created_at` timestamps (NOT relying on Postgres `now()`,
          which can collide across closely-spaced inserts). Use
          `created_at=datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)`,
          `…12, 0, 1`, `…12, 0, 2`, etc. — one per row, ascending,
          so the `created_at ASC` tie-break is observable.
        - Fixture distribution exercises BOTH directions of LLM-vs-
          operator disagreement (operator-says-True with
          confidence=low → alias-says-False, MISMATCH; operator-
          says-False with confidence=high → alias-says-True,
          MISMATCH; plus matches). Concrete:
            row1: confidence=low,    label_is_real_bug=True  (mismatch)
            row2: confidence=low,    label_is_real_bug=False (match)
            row3: confidence=medium, label_is_real_bug=True  (match)
            row4: confidence=high,   label_is_real_bug=False (mismatch)
            row5: confidence=high,   label_is_real_bug=True  (match)
          Expected: `label_accuracy = 3/5 = 0.6`.
        - Walk `GET /api/v1/review/triage-finding/next?limit=5` BEFORE
          labeling: with all 5 unlabeled and the deterministic
          timestamps above, assert order is `[row1, row2, row3,
          row4, row5]` (low → low → medium → high → high; ties on
          confidence broken by `created_at ASC`).
        - For each row, `POST /api/v1/review/triage-finding/{id}/label`
          with the fixture's ground-truth.
        - After the loop, `GET /api/v1/review/triage-finding/stats`
          returns `{total: 5, unlabeled: 0, labeled_total: 5,
          label_accuracy: 0.6, accuracy_last_100: 0.6}`.
        - Mutate row4's label to `label_is_real_bug=True` (now
          alias-matches), re-`GET /stats`, assert
          `label_accuracy = 0.8`.

      WIRE the framework's accuracy widget contract:
        - Verify substep 1's `useReviewStats(kind)` hook substitutes
          the kind via path-substitution and that
          `useReviewStats('triage-finding')` hits
          `GET /api/v1/review/triage-finding/stats`. If substep 1's
          hook is missing or mis-shaped, surface that as a substrate
          gap and STOP — do NOT add a viewer-side override hook
          (that would re-introduce the parallel surface this substep
          is supposed to eliminate).
        - Add a dashboard-side test
          `services/dashboard/src/review/viewers/triage-finding-stats.test.tsx`
          that mounts the framework's `<ReviewKind kind='triage-finding'>`
          (or `<ConfidenceStrip>` + a mocked `useReviewStats`) with
          a fetch mock returning the stats shape and asserts the
          rendered accuracy widget shows the percentage. Mounting
          the framework page (not just the viewer) is what exercises
          the kind-to-component + stats-hook plumbing end-to-end.

      Update both AGENT.md files (`services/api/AGENT.md` +
      `services/dashboard/AGENT.md`) to reference the integration
      test + the end-to-end loop as the substep-2 "abstraction-
      proof-on-existing-surface" deliverable.

      Do NOT delete the legacy `routers/triage/` endpoints or the
      legacy query hooks. That deprecation lands in substep 4.

      Do NOT add migrations or schema changes. The success criteria
      explicitly forbid it for this substep.
    scope:
      files:
        - services/api/tests/test_review_triage_finding_loop.py
        - services/dashboard/src/review/viewers/triage-finding-stats.test.tsx
        - services/api/AGENT.md
        - services/dashboard/AGENT.md
      services_affected:
        - services/api
        - services/dashboard
      out_of_scope:
        - services/api/treadmill_api/routers/triage/
        - services/api/treadmill_api/routers/review/review_triage_finding.py
        - services/api/treadmill_api/models/triage_finding.py
        - services/dashboard/src/review/viewers/triage-finding.tsx
        - services/dashboard/src/pages/
        - services/dashboard/src/api/review_queries.ts
    validation:
      # The dashboard stats-widget test runs on the PR's CI vitest
      # job, not in the worker sandbox (no dashboard node_modules in
      # workers/agent/Dockerfile). The in-worker gate is file-
      # existence + grep only.
      - kind: deterministic
        description: |
          The end-to-end loop test passes — next → label → stats
          math (including the symmetric LLM-mismatch fixture and the
          0.6 → 0.8 accuracy delta after a single label mutation).
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          [ -f "$ROOT/services/api/tests/test_review_triage_finding_loop.py" ]
          cd "$ROOT/services/api" && uv run pytest tests/test_review_triage_finding_loop.py -q
      - kind: deterministic
        description: |
          The dashboard stats-widget test file exists and references
          the framework's stats hook (or ConfidenceStrip), the
          triage-finding kind, and the /stats endpoint path.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          FILE="$ROOT/services/dashboard/src/review/viewers/triage-finding-stats.test.tsx"
          [ -f "$FILE" ]
          grep -q "triage-finding" "$FILE"
          grep -qE "useReviewStats|ConfidenceStrip|ReviewKind" "$FILE"
          grep -q "/stats" "$FILE"
      - kind: deterministic
        description: |
          Both AGENT.md files reference ADR-0070 and the substep-2
          integration deliverable.
        script: |
          set -euo pipefail
          ROOT="$(git rev-parse --show-toplevel)"
          grep -qE "ADR-0070" "$ROOT/services/api/AGENT.md"
          grep -qE "ADR-0070" "$ROOT/services/dashboard/AGENT.md"
          grep -qE "substep 2|review_triage_finding|integration" "$ROOT/services/api/AGENT.md"
```

## Diagram

Not applicable. ADR-0070 carries the canonical pipeline diagram
(LLM-as-judge → review queue → flip-through → label sink → corpus)
that this substep implements on the triage-finding surface.

## Risks / unknowns

- **Substep 1 contract drift.** Tasks B and C depend on the shape the
  framework substrate substep-1 lands (the `<ReviewPageLayout>` chrome,
  the `import.meta.glob` registry, the `useReviewQueue` / `useReviewStats`
  hooks). If substep 1's API differs from what this plan assumes, Task B
  adapts the viewer's exports to match — the framework's contract wins.
  Mitigation: the worker reads
  `services/dashboard/src/review/_framework.tsx` first thing in Task B's
  STUDY block and only then writes the viewer.
- **Confidence-as-LLM-label alias is a v1 simplification.** The existing
  `triage_findings` table has `confidence` (low/medium/high) but no
  `llm_label` column. v1 stats math treats `confidence != 'low'` as the
  detector's implicit "is real bug" claim. This is a documented
  approximation, NOT the long-term accuracy metric — substep 3 introduces
  per-kind `llm_label` columns alongside new kinds, and at that point
  the v2 enum-mapping refactor lifts this alias. Mitigation: the alias
  is documented in the new module's docstring with an explicit TODO
  marker; downstream consumers (Wave 4 / DSPy) read corpus exports, not
  the live `/stats` endpoint, so a slightly approximate v1 widget
  doesn't propagate into corpus-quality decisions.
- **Legacy endpoints staying live during the transition.** Task A
  leaves `routers/triage/labels.py` + `findings.py` mounted alongside
  the new `routers/review/` aggregator. Two endpoints reading + writing
  the same table is intentional for one release; the operator's URL
  redirect (`/triage` → `/review/triage-finding`) shifts dashboard
  traffic immediately, and the role-ui-triage POST-ingest path keeps
  hitting `/api/v1/triage/findings` until substep 4 renames it.
  Mitigation: deprecation TODO + docstring annotations make removal
  mechanical when substep 4 runs.
- **Order-by on text confidence column.** `confidence` is a CHECK-
  constrained text column ('high' | 'medium' | 'low'). Raw `ORDER BY
  confidence ASC` lexically sorts as `high → low → medium`, which is
  wrong. Mitigation: Task A's intent calls out the CASE-based ordering
  explicitly and a test asserts the response order, not just the SQL
  string. Worker MUST add `ORDER BY CASE confidence WHEN 'low' THEN 0
  WHEN 'medium' THEN 1 WHEN 'high' THEN 2 END ASC, created_at ASC`.
- **Stats query performance on a large corpus.** The `accuracy_last_100`
  calculation requires a `labeled_at DESC LIMIT 100` subquery. Without
  an index on `labeled_at`, this scans the table. v1 corpus is small
  (low hundreds at time of writing) so this isn't a real risk yet;
  substep 4 / 5 can add the index when the corpus grows. Mitigation:
  Task A documents the index gap; no migration in this substep.
- **Viewer test setup.** `services/dashboard/src/review/triage-
  finding.test.tsx` is the first test under the new `src/review/`
  directory. Vitest's default test discovery should pick it up
  (config is in `vite.config.ts` / `vitest.config.ts`), but if it
  doesn't, the worker may need to update the includes glob. Mitigation:
  Task B's validation script explicitly invokes the test by path
  (`npm test -- --run src/review/triage-finding.test.tsx`), so a
  glob omission surfaces immediately as a "no tests found" failure.

## Decisions captured during execution

(empty at draft time)

## Post-mortem

(filled when plan transitions to completed)
