---
auto_merge: false
---

# Plan: ADR-0059 Step 1 — worker_deps schema + HTTP

- **Status:** active
- **Date:** 2026-05-28
- **Related ADRs:** ADR-0059, ADR-0050 (onboarding persistence), ADR-0055
  (per-account Claude credentials — shape this mirrors)
- **Supersedes:** none

## Goal

Ship the first slice of ADR-0059 — the per-repo `worker_deps`
registration. Step 1 is the API-side surface: a `WorkerDeps` Pydantic
model, a migration adding two ARRAY columns + one new table, the
onboarding HTTP endpoint accepting + returning the new field, and the
`OnboardingStore` round-trip. Worker materialization (Step 2) and
egress scoping (Step 3) follow as separate PRs.

The bunkhouse-precedent audit called out in the ADR's "Alternatives
considered" was resolved 2026-05-28: bunkhouse doesn't have a
precedent for per-repo deps — this is new ground. The shape sketched
in the ADR (TEXT[] arrays for Python/Node + a side table for binaries,
mirroring ADR-0050's onboarding pattern and ADR-0054's context-docs
pattern) is the canonical shape. As part of this task the worker also
flips the ADR-0059 status header from `proposed` to `accepted`.

`auto_merge: false` for the same reason as ADR-0058's finalize plan —
a concurrent orchestrator is active; human-merge to avoid cross-session
conflict.

## Success criteria

- A migration adds `repo_configs.worker_deps_python TEXT[]` and
  `repo_configs.worker_deps_node TEXT[]` (nullable, defaulting to
  empty arrays so existing rows remain valid).
- A migration creates `repo_worker_binaries(id, repo_config_id FK,
  name, download_url, sha256_checksum, target_path, created_at)`.
- A `WorkerDeps` Pydantic model + a `BinarySpec` model live in
  `services/api/treadmill_api/models/onboarding.py` (or sibling).
  Models are strict (`extra='forbid'`) and round-trip cleanly.
- `OnboardingStore.upsert_repo_config` accepts a `WorkerDeps` and
  persists all three pieces; `get_repo_config` returns them.
- `POST /api/v1/onboarding/repos` accepts an optional `worker_deps`
  field; `GET /api/v1/onboarding/repos/{repo:path}` returns it.
- Unit tests cover: model round-trip, store round-trip, HTTP round-trip,
  empty-list defaults, binary-spec checksum validation.
- ADR-0059's status header flips from `proposed` to `accepted`.
- `services/api/AGENT.md` gains a Recent-changes entry per ADR-0030.

## Constraints / scope

### In scope

- Schema: migration + Pydantic models.
- `OnboardingStore` round-trip for the new fields.
- HTTP accept/return at the onboarding endpoint.
- Unit tests (no integration test against live Postgres yet — covered
  by Step 2's smoke).
- ADR-0059 status flip.
- AGENT.md update.

### Out of scope

- Worker-side materialization (Step 2 of ADR-0059, separate plan).
- Egress scoping (Step 3 of ADR-0059, separate plan).
- wf-discover auto-detection of deps (separate plan).
- Apt / OS-package support (deferred per ADR-0059 to v2).
- CLI surface for operator updates (Step 5 of ADR-0059, separate plan).

### Budget

One worker dispatch. If the task wedges (architect-amend cap fires
before merge), the structural test of post-ADR-0058 reliability is
the more interesting signal — surface to operator, don't grind.

## Sequence of work

```yaml
sequence_of_work:
  - id: adr-0059-step-1-worker-deps-schema
    title: "ADR-0059 Step 1 — worker_deps Pydantic models + migration + HTTP round-trip"
    workflow: wf-author
    intent: |
      STUDY: read these as shape references —
        - `services/api/treadmill_api/models/onboarding.py` (the
          existing `RepoConfigRow` + `claude_account` field added in
          ADR-0055)
        - `services/api/alembic/versions/20260526_1500_repo_configs_claude_account.py`
          (the migration shape — small, additive)
        - `services/api/treadmill_api/onboarding_store.py` (how
          `OnboardingStore.upsert_repo_config` round-trips fields
          via direct attribute assignment)
        - `services/api/treadmill_api/routers/onboarding.py` (how
          the HTTP layer accepts + returns RepoConfig fields)
        - `docs/adrs/0059-per-repo-worker-deps-registration.md` (the
          decision + shape — read the "Decision" + "Shape" sections;
          they specify TEXT[] arrays for Python/Node + a side table
          for binaries; mirror ADR-0050 + ADR-0054 patterns).

      BUILD:

      (1) New Pydantic models in
          `services/api/treadmill_api/models/onboarding.py`:
          - `WorkerDeps` (`extra='forbid'`):
            * `python: list[str] = []`
            * `node: list[str] = []`
            * `binaries: list[BinarySpec] = []`
          - `BinarySpec` (`extra='forbid'`):
            * `name: str` (min_length=1)
            * `download_url: str` (HTTP URL; basic-URL validation OK,
              not a full validator)
            * `sha256_checksum: str` (must be 64 lowercase hex chars
              — add a Pydantic `field_validator`)
            * `target_path: str` (min_length=1, must start with
              `/var/treadmill/repo-bin/` per the ADR's
              materialization spec)

      (2) New migration at
          `services/api/alembic/versions/<NEW_DATESTAMP>_repo_configs_worker_deps.py`:
          - `down_revision = "20260526_1500"` (after the
            claude_account migration; verify by reading
            `services/api/alembic/versions/` to confirm the latest
            head).
          - upgrade():
              ```python
              op.add_column(
                  "repo_configs",
                  sa.Column("worker_deps_python", sa.ARRAY(sa.Text()),
                            nullable=False, server_default="{}"),
              )
              op.add_column(
                  "repo_configs",
                  sa.Column("worker_deps_node", sa.ARRAY(sa.Text()),
                            nullable=False, server_default="{}"),
              )
              op.create_table(
                  "repo_worker_binaries",
                  sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                            primary_key=True,
                            server_default=sa.text("gen_random_uuid()")),
                  sa.Column("repo_config_id", sa.dialects.postgresql.UUID(as_uuid=True),
                            sa.ForeignKey("repo_configs.id", ondelete="CASCADE"),
                            nullable=False),
                  sa.Column("name", sa.String(255), nullable=False),
                  sa.Column("download_url", sa.Text(), nullable=False),
                  sa.Column("sha256_checksum", sa.String(64), nullable=False),
                  sa.Column("target_path", sa.String(512), nullable=False),
                  sa.Column("created_at", sa.dialects.postgresql.TIMESTAMP(timezone=True),
                            nullable=False, server_default=sa.text("now()")),
              )
              op.create_index(
                  "ix_repo_worker_binaries_repo_config_id",
                  "repo_worker_binaries", ["repo_config_id"],
              )
              ```
          - downgrade(): inverse — drop table, drop the two columns.

      (3) New `RepoWorkerBinaryRow` SQLAlchemy model in
          `services/api/treadmill_api/models/onboarding.py` mirroring
          the `RepoConfigRow` shape.

      (4) Extend `RepoConfigRow` with two new `Mapped[list[str]]` columns
          (`worker_deps_python`, `worker_deps_node`).

      (5) `OnboardingStore` updates in `onboarding_store.py`:
          - `upsert_repo_config(config: RepoConfig)` accepts a
            `WorkerDeps | None` on `config`; persists arrays + binaries
            (insert/replace the binaries rows for the repo on each
            upsert — simpler than diffing; small list).
          - `get_repo_config(repo)` returns a `RepoConfig` with the new
            `worker_deps` field populated (or `WorkerDeps()` if all
            three lists are empty — never return `None` for the field).

      (6) Router updates in `routers/onboarding.py`:
          - `POST /api/v1/onboarding/repos` request schema accepts
            optional `worker_deps: WorkerDeps | None`; defaults to
            `WorkerDeps()` (all empty) when omitted.
          - `GET /api/v1/onboarding/repos/{repo:path}` response shape
            includes the `worker_deps` field.

      (7) Edit `docs/adrs/0059-per-repo-worker-deps-registration.md`:
          flip the status header from `proposed` to `accepted`. Add a
          line in the "Alternatives considered" / bunkhouse section
          recording the 2026-05-28 resolution: "bunkhouse doesn't
          have a precedent for per-repo deps — confirmed by operator
          2026-05-28".

      (8) AGENT.md updates in `services/api/AGENT.md`:
          - Extend the `onboarding_store.py` key-surfaces line to
            mention `worker_deps` round-trip.
          - Add a Recent-changes entry citing ADR-0059 Step 1.

      TESTS:
      Add `services/api/tests/test_worker_deps_models.py` covering:
        - `WorkerDeps()` round-trips with all empty lists.
        - `BinarySpec` validates 64-hex checksum (accepts lowercase,
          rejects uppercase, rejects short/long, rejects non-hex).
        - `BinarySpec` rejects target_path that doesn't start with
          `/var/treadmill/repo-bin/`.
      Add coverage in `services/api/tests/test_onboarding_store.py`
      (extend existing tests) for the round-trip:
        - upsert with worker_deps → get returns the same structure
        - upsert with no worker_deps → get returns `WorkerDeps()`
        - re-upsert replaces binaries (simple "drop + insert" path)
      Add coverage in `services/api/tests/test_routers_onboarding.py`
      (extend existing tests) for HTTP round-trip.

      Validation MUST NOT use `cdk synth`, `docker`, live AWS, or
      network egress. The migration runs against the test Postgres
      (already in the pytest fixtures); pytest is sandbox-safe.
    scope:
      files:
        - services/api/treadmill_api/models/onboarding.py
        - services/api/treadmill_api/onboarding_store.py
        - services/api/treadmill_api/routers/onboarding.py
        - services/api/alembic/versions/20260528_1600_repo_configs_worker_deps.py
        - services/api/tests/test_worker_deps_models.py
        - services/api/tests/test_onboarding_store.py
        - services/api/tests/test_routers_onboarding.py
        - docs/adrs/0059-per-repo-worker-deps-registration.md
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - Worker-side materialization (separate plan — ADR-0059 Step 2)
        - wf-discover changes (separate plan)
        - CLI changes (separate plan)
    validation:
      - kind: deterministic
        description: New model tests + extended onboarding + router tests + full services/api suite stay green.
        script: |
          cd services/api && uv run pytest tests/test_worker_deps_models.py tests/test_onboarding_store.py tests/test_routers_onboarding.py -q
        severity: blocking
        timeout_seconds: 180
      - kind: deterministic
        description: A new migration file exists somewhere under alembic/versions/ that adds the worker_deps columns or the repo_worker_binaries table. Robust to filename / timestamp variation (per SKILL.md "make deterministic validation robust, not formatting-brittle" — the prior exact-filename gate caused a brittle wedge).
        script: |
          grep -lE 'worker_deps_python|repo_worker_binaries' services/api/alembic/versions/*.py | head -1
        severity: blocking
        timeout_seconds: 30
      - kind: llm-judge
        description: ADR-0059 status flipped + AGENT.md updated per ADR-0030.
        prompt: |
          The DIFF should:
            (1) flip ADR-0059's status header from 'proposed' to 'accepted', AND
            (2) include an AGENT.md Recent-changes entry in services/api/AGENT.md
                citing ADR-0059 Step 1.
          Return verdict 'pass' when both are present; 'fail' otherwise.
        severity: blocking
```

## Risks / unknowns

- **Migration numbering collision.** If the worker authors a date-
  stamp that's earlier than another in-flight migration, alembic
  will refuse to apply. Tasks at this level conventionally use
  `<YYYYMMDD>_<HHMM>_<slug>` matching their dispatch date; the
  scope.files entry uses `20260528_1600_...` as a placeholder, but
  the worker should verify the actual latest head before naming.
- **The `target_path` constraint is opinionated.** Forcing
  binaries into `/var/treadmill/repo-bin/<repo>/<name>` matches
  ADR-0059's materialization spec but may need relaxation when
  Step 2 ships. If so, fix the constraint there.
- **We'll abort if** the migration introduces a CHECK constraint
  that breaks the existing test fixtures (some tests don't pin
  alembic head, so a strict default could surprise them). Drop the
  constraint to nullable + default to `'{}'` server-side if so.

## Decisions captured during execution

(empty)

## Post-mortem

(filled in on completion / abandonment)
