# rule:alembic-migration-runnable

Deterministic gate for ADR-0080. Runs whenever a PR touches a file
under `services/api/alembic/versions/` (or always, if no changed-files
list is supplied — fail-safe).

## What it catches

Two failure modes seen in production on the 2026-06-05 ADR-0076 PR A
worker iteration:

1. **TypeError-shaped misuse of `op.*` calls.** Example: the worker
   shipped

   ```python
   op.create_check_constraint(
       "ck_repo_configs_git_author_paired",
       "(git_author_name IS NULL) = (git_author_email IS NULL)",
       table_name="repo_configs",
   )
   ```

   The actual alembic signature is
   `create_check_constraint(constraint_name, table_name, condition)` —
   the condition string lands in the `table_name` positional slot and
   the `table_name=` kwarg duplicates → `TypeError: got multiple values
   for argument 'table_name'`. The function body raises during the DDL
   generation pass, no SQL is emitted, but a sandbox without a real DB
   can't tell schema-shape tests anything went wrong. This gate runs
   `alembic upgrade --sql head` which surfaces the error immediately
   via the non-zero exit and empty DDL output.

2. **Multi-head collisions.** Two migrations chained off the same
   parent → two alembic heads. Detected via
   `alembic heads --resolve-dependencies` counting `(head)`-tagged
   entries. Caught here pre-merge instead of post-CI.

## Two gates

| Gate | Command | Failure signal |
|---|---|---|
| 1. Single head | `alembic heads --resolve-dependencies` | Output contains > 1 `(head)` line |
| 2. Upgrade runs cleanly | `alembic upgrade --sql head` | Non-zero exit OR no `CREATE|ALTER|INSERT|DROP` keywords in the output |

## Offline mode

Gate 2 uses `--sql` which alembic resolves in **offline mode** — no DB
connection. `services/api/alembic/env.py::_resolve_sync_url()` returns
a placeholder dialect URL when `DATABASE_URL` is unset AND
`context.is_offline_mode()` is true, letting the gate run in the
worker sandbox without a live DB.

## Invocation contracts

The script accepts a changed-files list in priority order:

1. `$1` is a path to a newline-delimited file list (test-harness
   contract used by `test_check.sh`).
2. `CHANGED_FILES` env var holds the same shape (validation-runtime
   contract).
3. Neither set — run both gates unconditionally (CI / manual run).

If a changed-files list IS supplied and contains no path under
`services/api/alembic/versions/`, the script exits 0 immediately
(no-op short-circuit).

## Running locally

```
./tools/rule-checks/alembic-migration-runnable/check.sh
```

Exit 0 = both gates pass. Exit 1 = either gate failed; stderr names
which one + a remediation hint.

## Test suite

```
./tools/rule-checks/alembic-migration-runnable/test_check.sh
```

Covers happy path + each failure mode + the no-op short-circuit.
