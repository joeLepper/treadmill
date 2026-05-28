---
auto_merge: false
---

# Plan: role-ui-triage Step 1 — `triage_findings` schema + storage + seed corpus

- **Status:** active
- **Date:** 2026-05-28
- **Related ADRs:** ADR-0061 (the architectural decision)
- **Parent plan:** `docs/plans/2026-05-28-role-ui-triage.md` — this is Step 1.
- **Builds on:** main; no other steps yet.

## Required reading

1. `docs/adrs/0061-role-ui-triage-labelable-visual-bug-detection.md` —
   the schema's three-layer shape (provenance / evidence / decision)
   plus labels, the closed enums, and the retention policy.
2. `docs/triage/role-ui-triage.v1.md` — the v1 prompt; the schema in
   this step is the contract that prompt's output must satisfy.
3. `services/api/treadmill_api/models/event.py` and any sibling
   model — for the SQLAlchemy convention this repo uses (declarative
   `Mapped`-typed columns, alembic migrations under
   `services/api/alembic/versions/`).
4. `services/api/treadmill_api/onboarding_store.py` — the
   repository-pattern template the new `triage_store.py` should
   follow.

## sequence_of_work

```yaml
sequence_of_work:
  - id: triage-findings-schema-storage-seed
    title: triage_findings table + Pydantic + repository + seed corpus
    workflow: wf-author
    intent: |
      Land the schema, model, repository, and seed corpus required
      for ADR-0061's role-ui-triage. Everything in Steps 2-6 depends
      on this; nothing else in the rollout can land first.

      REQUIRED READING (do this first):
      1. docs/adrs/0061-role-ui-triage-labelable-visual-bug-detection.md
      2. docs/triage/role-ui-triage.v1.md — the prompt artifact whose
         output the schema must accept verbatim.
      3. services/api/treadmill_api/models/event.py and any sibling
         model in services/api/treadmill_api/models/ — SQLAlchemy
         conventions (Mapped, declarative_base, JSONB usage).
      4. services/api/treadmill_api/onboarding_store.py — the
         repository pattern triage_store should mirror.
      5. services/api/alembic/versions/ — the existing migrations to
         understand the alembic naming convention and revision chain.

      WHAT TO BUILD:

      A) services/api/treadmill_api/models/triage_finding.py — the
         SQLAlchemy model. Columns per ADR-0061 §"Schema":
           - finding_id (UUID, PK)
           - run_id (UUID, indexed)
           - created_at (TIMESTAMPTZ, default now())
           - prompt_version (TEXT, indexed)
           - model (TEXT)
           - mode (VARCHAR(16), check constraint: 'periodic' or 'on_demand')
           - on_demand_request (TEXT NULL)
           - target_url (TEXT, indexed)
           - viewport_w (INT)
           - viewport_h (INT)
           - git_sha (TEXT)
           - api_git_sha (TEXT NULL)
           - screenshot_uri / viewport_png_uri / dom_snapshot_uri /
             console_log_uri / network_log_uri (all TEXT;
             nullable except screenshot + console + network)
           - evidence_summary (JSONB)
           - category (VARCHAR(32), check constraint covering the
             9 enum values from the prompt: console_error,
             network_failure, broken_asset, accessibility,
             layout_overflow, consistency, dead_affordance,
             loading_state, other)
           - severity / confidence (VARCHAR(8), check: high/medium/low)
           - observation (TEXT, ≤240 chars enforced in Pydantic)
           - evidence_pointer (TEXT)
           - proposed_resolution (TEXT)
           - dispatch_action (VARCHAR(32), check: dispatched /
             research_only / suppressed / escalated_to_operator)
           - dispatch_reason (TEXT)
           - suppression_signal (VARCHAR(32) NULL, check covers the
             7 enum values from the prompt when not null)
           - parent_finding_id (UUID NULL, FK to triage_findings.finding_id
             ON DELETE SET NULL)
           - dispatched_plan_id (UUID NULL, FK to plans.id ON DELETE SET NULL)
           - outcome_state (VARCHAR(16) NULL, check: pending / merged /
             rejected / superseded / cancelled when not null)
           - outcome_pr_number (INT NULL)
           - outcome_merged_at (TIMESTAMPTZ NULL)
           - recurrence_count (INT NOT NULL DEFAULT 0)
           - label_is_real_bug (BOOL NULL)
           - label_severity (VARCHAR(8) NULL)
           - label_category (VARCHAR(32) NULL)
           - label_fix_in_dsl (BOOL NULL)
           - label_dispatch_action (VARCHAR(32) NULL)
           - label_notes (TEXT NULL)
           - labeled_by (TEXT NULL)
           - labeled_at (TIMESTAMPTZ NULL)
           - label_guidelines_version (TEXT NULL)

         Indexes: (run_id), (prompt_version), (target_url),
         (dispatch_action), (label_is_real_bug) WHERE label_is_real_bug
         IS NULL — the last one is a partial index so the labeling UI's
         "next unlabeled" query is constant-time.

      B) services/api/alembic/versions/<sha>_triage_findings.py —
         the migration. Use op.create_table with the columns above,
         the indexes, and the CHECK constraints. Follow the
         existing migration style (timestamped revision; depends_on
         the latest revision in main).

      C) services/api/treadmill_api/triage_store.py — the
         repository. Mirror onboarding_store.py's pattern (a class
         with async methods). Initial methods:
           - insert_finding(session, finding: TriageFinding) -> UUID
           - update_outcome(session, dispatched_plan_id: UUID,
             outcome_state: str, outcome_pr_number: int | None,
             outcome_merged_at: datetime | None) -> int  # rows updated
           - record_label(session, finding_id: UUID, ...labels) -> None
           - get_unlabeled_findings(session, limit: int = 50)
             -> list[TriageFinding]  # for the labeling UI

      D) services/api/treadmill_api/schemas/triage_finding.py
         (or wherever Pydantic models live) — the Pydantic v2
         model. Enforce:
           - observation max_length=240
           - proposed_resolution max_length=900
           - category Literal of the 9 enum values + "other"
           - severity / confidence Literal high/medium/low
           - dispatch_action Literal of the 4 enum values
           - suppression_signal Literal of the 7 enum values
             when set; null when dispatch_action != "suppressed"
             (model_validator)
           - validate that suppression_signal is null iff
             dispatch_action != "suppressed"
           - validate that dispatched_plan_id is null iff
             dispatch_action != "dispatched"

      E) docs/triage/seed-corpus.md — the bootstrap corpus. Format
         the 8 manually-triaged findings from the conversation
         (nginx-proxy-gone, terminal-filter, WS-403,
         escalation-strip-dominates, failed-states-bucket-as-inflight,
         deploy-watcher-not-recreating, real-RAMJAC-identifiers,
         body-vs-scroll) as a JSON array of TriageFindings, with
         label_is_real_bug + label_severity + label_dispatch_action
         + label_notes populated by the operator's actual decisions.
         These are the labels Joe's actions imply. The file is
         markdown wrapping a fenced ```json``` block; a future
         migration or a one-time CLI loader (out of scope for this
         step) imports it into the table. Reference for the
         resolution descriptions: the corresponding PRs (#49, #52,
         and the ones not-yet-fixed are described in DESIGN.md
         terms).

      F) services/api/tests/test_triage_store.py — covers:
           - insert_finding round-trip (Pydantic → row → Pydantic)
           - the suppression_signal validator catches an invalid
             combination
           - update_outcome with no matching dispatched_plan_id
             returns 0 rows updated
           - update_outcome with a match updates the row and is
             idempotent (re-running with the same args is a no-op)
           - get_unlabeled_findings excludes labeled rows
           - record_label sets the labels and labeled_at
           - all 9 category values + "other" are accepted; an
             unknown value rejected

      G) services/api/AGENT.md — "Recent changes" entry describing
         the new model + table + store + seed-corpus path.
         MANDATORY per ADR-0030 docs-current-with-pr; per
         [[feedback-architect-overrules-doc-rule]], call out
         explicitly in the diff.

      OUT OF SCOPE:
      - The S3 upload path (workers/agent or routers does the
        upload; Step 2 / Step 3 own that).
      - The coordination-consumer outcome hook (Step 4).
      - The labeling UI (Step 6).
      - The actual loading of seed-corpus.md into the table — that's
        either a one-time CLI command we add post-merge or a Step-6-
        adjacent task.

      SCOPE GUARDRAILS:
      - Do NOT add any new router. The labeling endpoints are Step 6.
      - Do NOT extend coordination/consumer.py. Outcome projection is
        Step 4.
      - Do NOT touch starters.py or the role/workflow seeds. Step 3.
      - Validation runs only the new test file + a smoke-import for
        app + an alembic-upgrade smoke; do NOT run the full
        services/api suite per
        [[feedback-worker-validation-script-scope]].
    scope:
      files:
        - services/api/treadmill_api/models/triage_finding.py
        - services/api/alembic/versions/<rev>_triage_findings.py
        - services/api/treadmill_api/triage_store.py
        - services/api/treadmill_api/schemas/triage_finding.py
        - services/api/tests/test_triage_store.py
        - docs/triage/seed-corpus.md
        - services/api/AGENT.md
    validation:
      - kind: deterministic
        description: |
          New test module passes; the API app builds without import
          errors; alembic upgrade against a fresh DB completes.
        script: |
          set -e
          cd services/api
          uv run pytest -q tests/test_triage_store.py
          uv run python -c "from treadmill_api.app import create_app; create_app()"
          # Alembic smoke: upgrade head against a fresh SQLite (the
          # services/api test config uses in-memory SQLite for non-async
          # paths; check the existing test_starters.py for the pattern
          # if Postgres-only constructs trip this).
          uv run alembic upgrade head
```
