---
auto_merge: false
---

# Plan: ADR-0076 implementation — per-repo git author override

- **Status:** drafting
- **Date:** 2026-06-05
- **Related ADRs:** ADR-0076 (the decision, PR #210 merged at 2c866063), ADR-0050 (onboarding + RepoConfig), ADR-0055 (mirror pattern — per-repo nullable override → deployment default), ADR-0049 (commit-vs-PR identity split)

## Goal

Land the three nullable `repo_configs` columns ADR-0076 specifies
(`git_author_name`, `git_author_email`, `commit_trailer`) and wire the
worker's commit path to honor them, so a repo can be onboarded with a
git author identity + trailer policy that overrides the worker's
defaults at every commit. Unblocks the ZEPHYR/zephyr bootstrap on
Treadmill's side; behaviour-neutral for every currently-onboarded repo
(all-NULL → existing defaults preserved).

## Success criteria

1. `repo_configs` has the three columns; CHECK constraint
   `((git_author_name IS NULL) = (git_author_email IS NULL))` is
   enforced at the database level.
2. `OnboardingStore.upsert_repo_config` / `get_repo_config` round-trip
   all three fields; `RepoConfig` dataclass carries them.
3. `POST /api/v1/onboarding/repos` accepts the three fields in the
   payload (omitted = NULL); `GET /api/v1/onboarding/repos/{repo}`
   returns them. The request Pydantic model rejects a payload that
   sets exactly one of `git_author_name` / `git_author_email`.
4. Worker's `_configure_local_identity` consults the per-repo override
   before falling back to env defaults; `_run_commit` (or the
   commit-message template that calls it) applies the
   `commit_trailer` three-valued semantics (NULL → default trailer,
   `""` → suppress, any string → use verbatim).
5. End-to-end smoke against `ZEPHYR/zephyr` with
   `git_author_name="Joe Lepper"` /
   `git_author_email="josephlepper@gmail.com"` / `commit_trailer=""`:
   a Treadmill worker commit shows `Joe Lepper
   <josephlepper@gmail.com>` as the author with no Co-Authored-By
   trailer. (Not executable in this plan's scope — it's the
   acceptance test for the implementation across both PRs.)

## Constraints / scope

### In scope

- New alembic migration adding the three columns + the CHECK.
- `RepoConfig` dataclass + `RepoConfigRow` ORM updates.
- `OnboardingStore` accessor updates.
- Onboarding router (Pydantic + endpoint) updates.
- Worker `git.py` + the runner_dispositions seam that calls
  `commit_all`.
- Tests at every layer (migration, store roundtrip, router, worker
  commit-config).
- `services/api/AGENT.md` and `workers/agent/AGENT.md` recent-changes
  entries per ADR-0030.

### Out of scope

- **PR-level identity.** Per ADR-0076 §Follow-ups, the
  treadmill-agent[bot] PR author stays as-is; the PAT-vs-App-install
  question for orgs without the App installed is a sibling ADR.
- **Operator CLI / dashboard UI** for the override fields. Schema +
  API first; UI later, separate plan.
- **Migrating existing rows.** All three columns default NULL; the
  existing `treadmill`, RAMJAC, RAMJAC-events, and ZEPHYR/zephyr
  rows are behaviour-neutral until an operator explicitly sets the
  override.
- **Changing the env-var fallback shape.** `GIT_AUTHOR_EMAIL` and
  `GIT_AUTHOR_NAME` continue to be the deployment-level default for
  any repo whose override columns are NULL. ADR-0055 mirrored the
  same pattern (deployment default → per-repo override).

### Budget

Two PRs, hand-authored. Estimated ~half-day total: API/schema/store/
router land first as PR A (behavior-neutral); worker commit-config
flow lands as PR B (consumes the new fields). PR B is the one that
makes commits actually carry the override. `auto_merge: false`
because both PRs touch shared schema + production paths that benefit
from human-eye review even with all-NULL behavior-neutrality.

## Sequence of work

```yaml
sequence_of_work:
  - id: schema-and-api
    title: "ADR-0076 PR A — schema + ORM + store + API surface"
    workflow: wf-author
    intent: |
      STUDY:
        - docs/adrs/0076-per-repo-git-author-override-on-repoconfig.md
          (the decision; the 3-valued commit_trailer semantics and the
          CHECK constraint shape are load-bearing)
        - services/api/treadmill_api/repo_config.py — the existing
          `RepoConfig` dataclass; mirror ADR-0055's `claude_account`
          nullable-override pattern (claude_account is precedent)
        - services/api/treadmill_api/onboarding_store.py — the
          `RepoConfigRow` ORM definition + `upsert_repo_config` and
          `get_repo_config` methods; this is the seam the new fields
          plug into
        - services/api/treadmill_api/routers/onboarding.py — the
          POST/GET route + Pydantic request/response models
        - services/api/alembic/versions/ — most recent migration file
          for naming convention (`YYYYMMDD_HHMM_<slug>.py`)
        - services/api/AGENT.md — the recent-changes section for the
          ADR-0030 docs-current-with-pr gate

      BUILD:
        1. New alembic migration `YYYYMMDD_HHMM_repo_config_git_author_override.py`
           adding:
             - `git_author_name: VARCHAR(255) NULL`
             - `git_author_email: VARCHAR(255) NULL`
             - `commit_trailer: TEXT NULL`
             - CHECK constraint `ck_repo_configs_git_author_paired`
               enforcing `(git_author_name IS NULL) = (git_author_email IS NULL)`
           Down migration drops the CHECK then the columns.
        2. `RepoConfigRow` ORM (in onboarding_store.py) gains the
           three columns with matching types + nullability + the
           sqlalchemy CheckConstraint declared on `__table_args__`.
        3. `RepoConfig` dataclass (repo_config.py) gains the three
           `str | None = None` fields. Order them after the existing
           `claude_account` / `claude_account_fallback` fields so the
           authorial pattern is consistent.
        4. `upsert_repo_config` writes them; `get_repo_config` reads
           them; the `to_dict` / `from_dict` helpers (if used) carry
           them.
        5. Onboarding router's `OnboardRepoRequest` Pydantic model
           gains the three fields (optional, all default `None`).
           `OnboardRepoResponse` likewise. A Pydantic
           `model_validator` rejects a payload with exactly one of
           `git_author_name` / `git_author_email` set (matches the
           DB CHECK semantically + gives a 422 instead of a 500).
        6. `services/api/AGENT.md` Recent-changes entry citing
           ADR-0076 PR A + the migration filename + the new fields.

      TEST:
        - `services/api/tests/test_repo_config.py` — round-trip the
          three new fields through `from_dict` / `to_dict`; assert
          they default to None.
        - `services/api/tests/test_onboarding_store.py` (new or
          existing) — `upsert_repo_config` accepts a payload with the
          three fields, `get_repo_config` returns them; insert with
          name-but-no-email raises IntegrityError citing the CHECK.
        - `services/api/tests/test_routers_onboarding.py` — POST
          accepts the three fields, response carries them; POST with
          only name (no email) returns 422 with a Pydantic-style
          error citing the paired-fields rule.

      AGENT.md UPDATE per ADR-0030 — services/api/AGENT.md only;
      worker side updates in PR B.

      Validation MUST NOT use cdk synth, docker, live AWS, or
      network egress. Focused pytest against the touched test files.
    scope:
      files:
        - services/api/alembic/versions/  # new migration
        - services/api/treadmill_api/repo_config.py
        - services/api/treadmill_api/onboarding_store.py
        - services/api/treadmill_api/routers/onboarding.py
        - services/api/tests/test_repo_config.py
        - services/api/tests/test_onboarding_store.py
        - services/api/tests/test_routers_onboarding.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - workers/agent/  # worker-side wiring is PR B
        - Operator CLI / dashboard UI surfaces
        - Migrating existing onboarded rows
    validation:
      - kind: deterministic
        description: |
          Migration upgrade + downgrade succeed on a fresh DB;
          all three test files pass; full services/api unit suite
          remains green.
        script: |
          cd services/api && uv run pytest tests/test_repo_config.py tests/test_onboarding_store.py tests/test_routers_onboarding.py -q
        severity: blocking
        timeout_seconds: 180
      - kind: llm-judge
        description: |
          AGENT.md Recent-changes carries an entry citing ADR-0076 PR A
          + the migration filename + the three new fields per ADR-0030.
        prompt: |
          The DIFF should include a Recent-changes entry in
          services/api/AGENT.md citing ADR-0076 PR A, naming the new
          migration filename, and naming the three new fields
          (git_author_name, git_author_email, commit_trailer). Return
          verdict 'pass' when all three are present; 'fail' otherwise.
        severity: blocking

  - id: worker-commit-config
    title: "ADR-0076 PR B — worker commit-config flow consumes the new fields"
    workflow: wf-author
    depends_on:
      - task.schema-and-api.pr_merged
    intent: |
      STUDY:
        - workers/agent/treadmill_agent/git.py — specifically
          `_configure_local_identity` (~line 265) which currently
          reads `GIT_AUTHOR_EMAIL` / `GIT_AUTHOR_NAME` env vars with
          defaults `agent@treadmill.local` and `Treadmill Agent`. This
          is the seam ADR-0076's per-repo override threads through.
          Also `commit_all` (~line 177) which is the actual `git
          commit` invocation site.
        - workers/agent/treadmill_agent/runner_dispositions/code.py
          and documentation.py + crystallization.py — every site that
          calls `git.commit_all(ctx.repo_dir, _commit_message(ctx.ctx))`.
          The commit-message template is where the Co-Authored-By
          trailer is appended today; the 3-valued `commit_trailer`
          override lands at this layer.
        - workers/agent/treadmill_agent/runner.py — the seam that
          already fetches RepoConfig for `claude_account` resolution.
          The new fields piggy-back on the same fetch so we don't
          double-query.
        - services/api/treadmill_api/repo_config.py — the RepoConfig
          shape PR A landed.

      BUILD:
        1. Extend `_configure_local_identity` to accept optional
           `author_name: str | None` and `author_email: str | None`
           parameters. When set, use them; otherwise fall back to the
           existing env-var path. Keep the function backward-
           compatible for tests that don't pass overrides.
        2. Extend `commit_all` to accept an optional `trailer: str |
           None = None` parameter following the three-valued semantics
           in ADR-0076:
             - `None` → keep the existing default trailer template
               (Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>)
             - `""` (empty string) → emit no trailer at all
             - any other string → use verbatim as the trailer line(s),
               appended to the message body with a blank line separator
           The default template lives in a single module-level constant
           so the suppression path is one branch, not a sed pass over
           the message.
        3. Identify the runner seam (probably in `runner.py` or a
           dispatched-context object) where RepoConfig is already
           resolved per-step. Add three lookups: `repo_config
           .git_author_name`, `.git_author_email`, `.commit_trailer`.
        4. Thread those into every `git.commit_all` call site
           (`runner_dispositions/code.py`, `documentation.py`,
           `crystallization.py`, anywhere else that emits a commit).
           The thread is `git.commit_all(repo_dir, message,
           author_name=..., author_email=..., trailer=...)`.
        5. `workers/agent/AGENT.md` Recent-changes entry citing
           ADR-0076 PR B + the `_configure_local_identity` /
           `commit_all` signature changes.

      TEST:
        - workers/agent/tests/test_git.py — extend or add tests:
          * `_configure_local_identity` with no overrides falls back
            to env vars (existing behavior preserved)
          * `_configure_local_identity` with overrides applies them
            via the expected git config args
          * `commit_all` with `trailer=None` emits the default
            Co-Authored-By trailer
          * `commit_all` with `trailer=""` emits NO trailer
          * `commit_all` with `trailer="Signed-off-by: X"` emits
            that line verbatim
        - workers/agent/tests/test_runner.py (or a focused new file)
          — confirm a RepoConfig with the three override fields set
          flows into `_configure_local_identity` + `commit_all` at
          the dispatching layer. May be a mock-heavy unit test
          rather than an end-to-end fixture.

      AGENT.md UPDATE per ADR-0030 — workers/agent/AGENT.md only;
      services/api AGENT.md was updated in PR A.

      Validation MUST NOT use cdk synth, docker, live AWS, or
      network egress. Focused pytest against the touched test files.
    scope:
      files:
        - workers/agent/treadmill_agent/git.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/treadmill_agent/runner_dispositions/code.py
        - workers/agent/treadmill_agent/runner_dispositions/documentation.py
        - workers/agent/treadmill_agent/runner_dispositions/crystallization.py
        - workers/agent/tests/test_git.py
        - workers/agent/tests/test_runner.py
        - workers/agent/AGENT.md
      services_affected:
        - workers/agent
      out_of_scope:
        - services/api/ surfaces (landed in PR A)
        - PR-level identity / GitHub App vs. PAT decision (sibling ADR)
        - End-to-end smoke against ZEPHYR/zephyr (acceptance only)
    validation:
      - kind: deterministic
        description: |
          Git + runner tests pass; existing dispositions suite remains
          green.
        script: |
          cd workers/agent && uv run pytest tests/test_git.py tests/test_runner.py tests/test_runner_dispositions.py -q
        severity: blocking
        timeout_seconds: 180
      - kind: llm-judge
        description: |
          AGENT.md Recent-changes carries an entry citing ADR-0076 PR B
          + the signature changes per ADR-0030.
        prompt: |
          The DIFF should include a Recent-changes entry in
          workers/agent/AGENT.md citing ADR-0076 PR B, naming the
          extended _configure_local_identity and commit_all
          signatures. Return verdict 'pass' when both present; 'fail'
          otherwise.
        severity: blocking
```

## Risks / unknowns

- **CHECK-constraint enforcement vs. ORM bulk inserts.** SQLAlchemy
  unit-of-work with explicit-PK rows can land in unexpected order;
  the CHECK is enforced at COMMIT, not at ADD, so a transaction that
  inserts a row with a half-populated pair will fail at commit time.
  Mitigation: the Pydantic-level paired-fields validator catches
  this BEFORE the DB write returns a 500; the CHECK is the durable
  backstop. Test pins both paths.
- **Commit-message trailer ergonomics.** A multi-line `commit_trailer`
  (e.g. `"Signed-off-by: A\nReviewed-by: B"`) should be emitted as
  two trailer lines, not one with a literal `\n`. The plan uses
  "verbatim" in the override semantics — clarify in code review of
  PR B that the empty-line-separator-before-trailer is preserved
  regardless of how many newlines the string contains.
- **Trailer policy interacts with the worker's existing message
  template.** The current default appends Co-Authored-By via the
  commit-message constructor; the override needs to either replace
  that whole template or override only the trailer slot. PR B's
  STUDY phase must locate exactly where the trailer is appended so
  the override hooks the right seam, not a downstream string-replace.
- **PR B's dependency on PR A merging.** If PR A churns in review,
  PR B is blocked; acceptable since `auto_merge: false` keeps both
  behind operator-eye and the sequence is short.

## Diagram

Reference ADR-0076's sequence diagram — the operator → API → DB
write-side + the worker's per-repo identity-resolution before
commit. No new flow introduced here; this plan operationalizes the
ADR-0076 contract.

## Decisions captured during execution

_Empty — populated as we work._

## Post-mortem

_Filled in on completed / abandoned: did the two PRs land clean? Did
the ZEPHYR/zephyr end-to-end smoke confirm the right commit identity?
Any surprises that should become an ADR or learning?_
