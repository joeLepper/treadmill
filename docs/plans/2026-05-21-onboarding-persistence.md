---
auto_merge: false
status: active
---

# Plan: Onboarding persistence — index tables + store (ADR-0050 wave 2)

- **Status:** active
- **Date:** 2026-05-21
- **Related ADRs:** ADR-0050 (onboarding), ADR-0011 (no metadata JSONB), ADR-0044 (datetime-keyed migrations), ADR-0030 (docs-current-with-pr)
- **Related plans:** 2026-05-21-onboard-unfamiliar-repos (wave 1 — the shapes)

## Goal

Persist the wave-1 onboarding shapes: a single Alembic migration creating the
`repo_configs`, `repo_profiles`, and `repo_context_docs` (the S3 index) tables,
their SQLAlchemy models, and an async `OnboardingStore` that reads/writes them
using the merged `RepoConfig` / `RepoProfile` dataclasses. This closes the loop
from wave-1's in-memory shapes to durable state.

**Manual-merge (`auto_merge: false`):** this touches shared schema and a live
startup path (migrations run at container boot). DB-backed tests are
`skipif(not TREADMILL_INTEGRATION)` and do **not** run in CI, so the operator
runs the integration suite + `alembic upgrade head` locally before merging.

## Success criteria

- One migration, single alembic head, creates the three tables with **typed
  columns + Postgres arrays — no JSONB** (ADR-0011 forbids metadata JSONB on
  repos).
- Models import and map the tables; `OnboardingStore` round-trips a `RepoConfig`
  and a `RepoProfile`, and `record_context_doc` bumps the version on re-record.
- `services/api/AGENT.md` reflects the new surfaces (ADR-0030 docs-currency) —
  authored *in this task* so it doesn't reproduce wave-1's review→architect dance.

## Constraints / scope

### In scope
The single task below — migration, models, accessor, tests, and the AGENT.md
update — built on the already-merged `repo_config.py` / `repo_profile.py`.

### Out of scope
Wiring the auto-merge block into the live trigger; router endpoints; the
`wf-discover` workflow + `role-cartographer`; context-provider wiring; CDK S3
bucket + IAM. Those are later waves / hand-driven.

### Budget
One task. If the migration can't be made single-head + the models can't import
cleanly under the deterministic check, it fails loudly rather than merging.

## sequence_of_work

```yaml
sequence_of_work:
  - id: onboarding-persistence
    title: Onboarding persistence — migration, models, store (ADR-0050)
    workflow: wf-author
    intent: |
      Persist the ADR-0050 onboarding shapes. Build on the ALREADY-MERGED
      ``treadmill_api/repo_config.py`` (``RepoConfig``, ``parse_repo_config``)
      and ``treadmill_api/repo_profile.py`` (``RepoProfile``, ``to_dict`` /
      ``from_dict``). Read both before starting.

      ADR-0011 forbids "metadata JSONB" on repos — use TYPED COLUMNS and
      Postgres ARRAYs for list fields; do NOT use JSONB anywhere here.

      (1) MIGRATION — create ONE new Alembic migration under
      ``services/api/alembic/versions/``. Per ADR-0044 the revision id is
      datetime-keyed ``YYYYMMDD_HHMM`` (use the authoring time, e.g.
      ``20260521_HHMM``). Set ``down_revision`` to the CURRENT single head —
      run ``cd services/api && uv run alembic heads`` and chain to whatever it
      reports (at authoring time it is ``20260520_0500``; verify). After adding
      the file, ``uv run alembic heads`` must still report exactly ONE head.
      Create three tables (match the existing migration style — ``op.create_table``
      with ``sa`` types, ``gen_random_uuid()`` server defaults, ``TIMESTAMP(timezone=True)``
      ``now()`` defaults; provide ``downgrade()`` that drops them in reverse):
        - ``repo_configs``: ``id`` UUID PK (gen_random_uuid); ``repo`` String(255)
          NOT NULL UNIQUE; ``mode`` String(16) NOT NULL server_default 'conform';
          ``auto_merge_blocked`` Boolean NOT NULL server_default false;
          ``test_command`` String NULL; ``lint_command`` String NULL;
          ``created_at``/``updated_at`` TIMESTAMP tz NOT NULL default now().
        - ``repo_profiles``: ``id`` UUID PK; ``repo`` String(255) NOT NULL UNIQUE;
          ``languages`` ARRAY(String) NOT NULL server_default '{}'; ``build_command``,
          ``test_command``, ``lint_command`` String NULL; ``doc_paths`` ARRAY(String)
          NOT NULL server_default '{}'; ``components`` ARRAY(String) NOT NULL
          server_default '{}'; ``ci`` String NULL; ``has_agent_context`` Boolean
          NOT NULL server_default false; ``created_at``/``updated_at`` TIMESTAMP tz.
        - ``repo_context_docs``: ``id`` UUID PK; ``repo`` String(255) NOT NULL;
          ``doc_path`` String NOT NULL; ``s3_key`` String NOT NULL; ``content_sha``
          String(64) NOT NULL; ``version`` Integer NOT NULL server_default '1';
          ``created_at`` TIMESTAMP tz NOT NULL default now(). Add a UNIQUE
          constraint on (``repo``, ``doc_path``, ``version``) and an index on
          (``repo``, ``doc_path``).

      (2) MODELS — new file ``treadmill_api/models/onboarding.py`` with
      ``RepoConfigRow``, ``RepoProfileRow``, ``RepoContextDocRow`` mapping those
      tables. Use the declarative ``Base`` from ``treadmill_api.database`` and
      ``mapped_column`` (match an existing model in ``treadmill_api/models/``,
      e.g. ``task.py``). Use ``sqlalchemy.ARRAY(sa.String)`` for the array
      columns. ``__tablename__`` must be exactly ``repo_configs`` /
      ``repo_profiles`` / ``repo_context_docs``. Register all three in
      ``treadmill_api/models/__init__.py`` (match how existing models are
      exported there) so they're importable and visible to Alembic.

      (3) ACCESSOR — new file ``treadmill_api/onboarding_store.py`` with a class
      ``OnboardingStore`` whose async methods take an ``AsyncSession`` (match the
      session-passing pattern in ``treadmill_api/coordination/``; the caller owns
      the session/transaction):
        - ``async upsert_repo_config(session, config: RepoConfig) -> None`` —
          insert, or update by ``repo`` if it exists.
        - ``async get_repo_config(session, repo: str) -> RepoConfig | None`` —
          return a ``RepoConfig`` dataclass (not the row) or None.
        - ``async upsert_repo_profile(session, profile: RepoProfile) -> None``.
        - ``async get_repo_profile(session, repo: str) -> RepoProfile | None`` —
          return a ``RepoProfile`` dataclass or None.
        - ``async record_context_doc(session, repo: str, doc_path: str,
          s3_key: str, content_sha: str) -> int`` — insert the next version for
          (repo, doc_path) (max existing version + 1, starting at 1) and return
          the new version.
        - ``async get_context_doc(session, repo: str, doc_path: str)
          -> RepoContextDocRow | None`` — the highest-version row for that pair.
      Import the dataclasses from ``treadmill_api.repo_config`` /
      ``treadmill_api.repo_profile`` and convert to/from the ORM rows.

      (4) TESTS — new file ``services/api/tests/test_onboarding_store.py``:
        - A NON-DB structural test (always runs): import the three models and
          assert their ``__tablename__`` values; assert ``OnboardingStore`` has
          the six methods above (``hasattr``).
        - INTEGRATION tests guarded by
          ``@pytest.mark.skipif(not os.environ.get("TREADMILL_INTEGRATION"),
          reason="set TREADMILL_INTEGRATION=1")`` using the real-Postgres
          ``session_factory`` fixture pattern from
          ``tests/test_integration_cross_step.py`` (copy that fixture shape):
          upsert+get a ``RepoConfig`` round-trips; upsert+get a ``RepoProfile``
          round-trips (lists preserved); ``record_context_doc`` returns 1 then 2
          on two calls for the same (repo, doc_path).

      (5) DOCS (ADR-0030 docs-current-with-pr — REQUIRED, do not skip) — update
      ``services/api/AGENT.md``: add a "Key surfaces" bullet for
      ``treadmill_api/onboarding_store.py`` + ``treadmill_api/models/onboarding.py``
      (the ADR-0050 onboarding persistence: repo config, discovery profile, and
      the S3 context-doc index), and a "Recent changes" bullet for this work.

      Do NOT wire anything live (no edits to the auto-merge trigger, app.py, or
      routers). Additive + the migration + the AGENT.md update only.
    scope:
      files:
        - services/api/alembic/versions/
        - services/api/treadmill_api/models/onboarding.py
        - services/api/treadmill_api/models/__init__.py
        - services/api/treadmill_api/onboarding_store.py
        - services/api/tests/test_onboarding_store.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/app.py
        - services/api/treadmill_api/coordination/triggers.py
    validation:
      - kind: deterministic
        description: |
          Single alembic head; the three models import with correct table
          names; the store class exists; the non-DB tests pass (integration
          tests skip without TREADMILL_INTEGRATION).
        script: |
          cd services/api \
            && [ "$(uv run alembic heads | grep -c '(head)')" = "1" ] \
            && uv run python -c "from treadmill_api.models.onboarding import RepoConfigRow, RepoProfileRow, RepoContextDocRow; assert RepoConfigRow.__tablename__=='repo_configs'; assert RepoProfileRow.__tablename__=='repo_profiles'; assert RepoContextDocRow.__tablename__=='repo_context_docs'" \
            && grep -q "class OnboardingStore" treadmill_api/onboarding_store.py \
            && uv run pytest tests/test_onboarding_store.py -q
```

## Diagram

No new actors — the persistence layer backs the store/profile in ADR-0050's
onboarding sequence diagram. See ADR-0050.

## Risks / unknowns

- **Migration head:** if the worker mis-chains `down_revision`, the deterministic
  check (single head) fails — caught before merge.
- **DB tests don't run in CI:** integration coverage is the operator's local
  pre-merge step (`TREADMILL_INTEGRATION=1 uv run pytest tests/test_onboarding_store.py`
  + `alembic upgrade head`), which is why this plan is manual-merge.
- **Array columns:** chosen over JSONB to honor ADR-0011; if a list field later
  needs richer structure that's a separate decision.

## Decisions captured during execution

- **Profile stored as typed columns + Postgres arrays, not JSONB** — ADR-0011
  forbids metadata JSONB on repos; refines ADR-0050's "JSONB" wording.
- **AGENT.md update scoped into the task** — wave-1's review→architect dance
  traced to under-scoped tasks omitting the ADR-0030 docs-currency surface;
  fixed at source here.

## Post-mortem

_(filled when the wave completes)_
