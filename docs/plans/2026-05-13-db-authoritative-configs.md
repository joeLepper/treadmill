---
status: active
trigger: ADR-0028 drafted 2026-05-12 in response to operator feedback on the bunkhouse-precedent failure mode. Open Qs Q28.a-e resolved 2026-05-13 (see ADR-0028 §"Resolved decisions"); coordinated alongside #108 + ADR-0027 in docs/plans/2026-05-13-in-session-sequencing.md.
parent: docs/adrs/0028-db-authoritative-workflow-configs.md
---

# Plan: DB-authoritative workflow / role / version configs (ADR-0028)

Flip the source of truth from `starters.py` to the DB. Code-side
definitions stay as fresh-deployment bootstrap fixtures; runtime
edits happen via API / CLI against the DB. Inverts the bunkhouse
"edit code, forget to seed, debug for 30min" failure mode.

## Goal

After this plan executes:

1. `starters.py` is **inert** for non-greenfield runtime updates.
   Editing a prompt in code has no effect on a running deployment.
2. Operators update prompts via a CLI subcommand (`treadmill role
   update <id> --prompt-from-file <path>`) that PUTs a new
   `role_versions` row. The DB row history is the audit trail.
3. `seed-starters` 409-no-op behavior becomes the **default**.
   `--reset-prompts-from-code` is the explicit opt-in for the
   recovery case.
4. Per Q28.a resolution: fresh deployments **auto-seed on first
   API startup** via `seed_starters_if_empty()` after alembic
   upgrade, gated by `SELECT … FOR UPDATE` on `alembic_version` for
   multi-replica safety.
5. `test_starters.py` shape invariants remain valuable — they are
   the spec for the bootstrap content, not for the runtime prompt
   set.

## Constraints / scope

### In scope
- `services/api/treadmill_api/starters.py` — the `seed()` function's
  409 behavior + the new `--reset-prompts-from-code` flag.
- `services/api/treadmill_api/routers/roles.py` — confirm a PUT path
  exists for prompt-only updates; add one if not.
- `services/api/treadmill_api/cli.py` — auto-seed-on-fresh-DB if
  Q28.a resolves to (ii).
- New CLI subcommands in `cli/treadmill_cli/cli.py`:
  `role show`, `role update`, `role versions` (rollback if Q28.b
  resolves yes).
- `services/api/tests/test_starters.py` — reframe assertions as
  bootstrap-content invariants, not runtime-state invariants.
- `docs/runbooks/` — new runbook on the role-edit workflow (folds
  into task #107).

### Out of scope
- Web UI for prompt editing (ADR-0028 §"Alternative C" — v1.5+).
- Workflow-shape edits via CLI (per Q28.e leaning — workflows stay
  code-driven).
- Per-deployment prompt overrides (separate concern; v0 has one
  prompt per role per deployment).
- Migrating existing deployments — for the personal deployment, an
  operator-triggered `--reset-prompts-from-code` is the migration.

## Sequence of work

```yaml
sequence_of_work:
  - id: audit-role-update-endpoint
    title: Audit + (if needed) implement the role PUT endpoint
    workflow: wf-author
    intent: |
      Read ``services/api/treadmill_api/routers/roles.py``. Confirm
      whether there is an endpoint that lets a CLI client update a
      role's ``system_prompt`` (and only that field — model + kind +
      other shape fields are out of scope here).

      If the endpoint exists: write a short note in the plan PR
      description naming the path + verb and proceed to the next
      task.

      If it doesn't: add a ``PATCH /api/v1/roles/{id}`` endpoint that
      accepts a partial body
      (``{system_prompt: str, notes: str | None}``) and creates a
      new ``role_versions`` row pointing to the role. The endpoint
      enforces:
        * ``role`` exists (404 if not).
        * ``system_prompt`` is non-empty.
        * The new version's ``version`` number is the previous
          max + 1 (DB unique constraint enforces; surface a 409 if
          a concurrent update raced).

      Tests: extend ``services/api/tests/test_routers_roles.py``
      (or sibling) with:
        * Happy path — PATCH → new ``role_versions`` row created.
        * 404 on unknown role.
        * 422 on empty system_prompt.
        * 409 on concurrent update (mock the unique-violation).
    scope:
      files:
        - services/api/treadmill_api/routers/roles.py
        - services/api/tests/test_routers_roles.py
    depends_on: []
    branch_hint: feat/role-patch-endpoint

  - id: seed-409-default-noop
    title: Make seed-starters' 409 path explicit + add reset flag
    workflow: wf-author
    intent: |
      In ``services/api/treadmill_api/starters.py``:

      1. Today the ``seed()`` function swallows 409s silently. Keep
         that behavior as the default but add a ``reset_from_code:
         bool = False`` parameter. When true, the function does a
         second pass: for each role / workflow / version where the
         API returned 409, issue a PATCH (or PUT) with the
         code-side content. Loud log: ``[red]RESET: overwriting %s
         from code-side definition (operator opted in via
         --reset-prompts-from-code)``.

      2. Update ``cli/treadmill_cli/cli.py``'s
         ``workflows seed-starters`` command to take a
         ``--reset-prompts-from-code`` flag and pass it through.
         Add an interactive confirmation when the flag is set —
         ``typer.confirm(...)`` with a message that lists how many
         existing rows would be overwritten. Skip the confirmation
         with ``--yes`` for scripted recovery.

      Tests: extend ``services/api/tests/test_starters.py`` with:
        * ``test_seed_default_does_not_overwrite_existing`` — pre-
          populate a role, call ``seed()``, confirm the row's
          ``system_prompt`` is unchanged.
        * ``test_seed_reset_from_code_overwrites_existing`` —
          pre-populate a divergent prompt, call ``seed(reset_from_code=True)``,
          confirm the row matches the code-side content.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - cli/treadmill_cli/cli.py
        - services/api/tests/test_starters.py
    depends_on:
      - task.audit-role-update-endpoint.pr_merged
    branch_hint: feat/seed-reset-flag

  - id: cli-role-subcommands
    title: Add treadmill role {show,update,versions} subcommands
    workflow: wf-author
    intent: |
      In ``cli/treadmill_cli/cli.py``, add a new ``role`` typer
      subapp (sibling to ``plan``, ``task``, ``workflows``) with:

      * ``treadmill role show <id> [--version N]`` — GET the role
        (or a specific version); print prompt + model + kind.
      * ``treadmill role update <id> --prompt-from-file <path>
        [--notes TEXT]`` — read the file, PATCH the endpoint,
        print the new version number.
      * ``treadmill role versions <id>`` — list role_versions rows
        with version + created_at + created_by + notes (if present).

      Rollback (``treadmill role rollback <id> --to-version N``)
      **deferred** per Q28.b resolution — not v1. Revisit when a
      forcing function arises (e.g., a botched edit needs reverting
      and the operator-grade UX of "rollback" beats "update with
      the prior version's content").

      Tests: ``tests/test_cli_role.py`` (new file) using
      ``typer.testing.CliRunner`` + a mocked API client. Assert:
        * ``show`` GETs the right path + prints prompt.
        * ``update`` POSTs the file contents + prints the new
          version number.
        * ``versions`` lists rows.
    scope:
      files:
        - cli/treadmill_cli/cli.py
        - cli/tests/test_cli_role.py
    depends_on:
      - task.seed-409-default-noop.pr_merged
    branch_hint: feat/cli-role-subcommands

  - id: auto-seed-on-fresh-db
    title: Auto-seed starters when the DB is empty
    workflow: wf-author
    intent: |
      Per Q28.a resolution (option (ii)): API auto-seeds on first
      startup against a fresh DB.

      In ``services/api/treadmill_api/cli.py``, after the alembic
      upgrade succeeds, check if the roles table is empty. If yes:
      call ``starters.seed(api_client)`` and log
      ``[green]seeded N starter roles + M workflows on first
      startup``. If no: silent.

      Race-safety: today's dev-local has one API replica, but
      future fully_remote may have N. Use a SELECT-FOR-UPDATE on a
      sentinel row (e.g., ``alembic_version``) to serialize the
      seed across replicas. Test the race with a thread pool.

      Tests: ``services/api/tests/test_cli_autoseed.py`` (new).
    scope:
      files:
        - services/api/treadmill_api/cli.py
        - services/api/tests/test_cli_autoseed.py
    depends_on:
      - task.cli-role-subcommands.pr_merged
    branch_hint: feat/auto-seed-fresh-db

  - id: runbook-role-edits
    title: Operator runbook for the new role-edit workflow
    workflow: wf-author
    intent: |
      Write ``docs/runbooks/edit-a-role-prompt.md`` covering:

      1. The new mental model: code-side ``starters.py`` is a
         bootstrap fixture, not a runtime spec. Editing it has no
         effect on running deployments.
      2. The supported edit workflow:
         a. Export the current prompt:
            ``treadmill role show role-reviewer > /tmp/prompt.md``
         b. Edit it.
         c. PUT the new version:
            ``treadmill role update role-reviewer --prompt-from-file
            /tmp/prompt.md --notes "reduce false-positive
            request_changes verdicts"``
         d. Verify:
            ``treadmill role show role-reviewer`` shows the new
            content.
         e. Watch the next wf-review run to confirm the new prompt
            took effect.
      3. The recovery path: if the DB diverges from what the
         operator expects (e.g., a bad edit went in), run
         ``treadmill workflows seed-starters
         --reset-prompts-from-code --yes`` to reset.
      4. Why this design (linking ADR-0028 for the rationale).

      Folds into the runbook reorganization (task #107).
    scope:
      files:
        - docs/runbooks/edit-a-role-prompt.md
    depends_on:
      - task.cli-role-subcommands.pr_merged
    branch_hint: docs/runbook-role-edits
```
