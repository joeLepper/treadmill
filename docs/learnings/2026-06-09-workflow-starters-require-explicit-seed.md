---
date: 2026-06-09
trigger: surprise
status: captured
related: ADR-0085+0086 combined implementation plan
---

# Learning: Workflow starters require explicit seed before plan submit

## Trigger

During ADR-0085+0086 bootstrap, `treadmill plan submit` failed with `error 400: workflow 'wf-author' not registered; register it via the workflows router first`. The DB was fresh (postgres volume reset overnight), and the seven canonical workflows had never been seeded into the `workflows` table.

## Observation

The `workflows` table is not auto-populated on container startup or alembic migration. A fresh postgres volume has an empty `workflows` table. `treadmill plan submit` checks for the named workflow on every submission and 400s if absent. Running `treadmill workflows seed-starters` before the first plan submit resolves it; output: `seeded: 13 new of 13 starter workflows (0 already existed)`.

The command is idempotent — safe to run on a populated DB.

## Generalization

Any environment that recreates the postgres volume (dev-local teardown/setup, CI, a new deployment target) needs `treadmill workflows seed-starters` before plans can be submitted. This is not documented in the startup runbook.

## Proposed rule

`treadmill workflows seed-starters` must run as part of any dev-local or test environment setup that starts from a fresh DB.

## Proposed remediation

Add `treadmill workflows seed-starters` to the `treadmill-local up` sequence (in `runtime.py` or the compose entrypoint health-check script) so it runs automatically after migration and before the API begins accepting traffic. Gate: idempotent, safe to run every startup.

## Notes

Related: the API binary path issue (`/home/joe/.local/bin/treadmill` used the wrong Python; fixed by symlinking to `.venv/bin/treadmill`) is a separate incident documented separately.
