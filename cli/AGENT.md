# cli

## Purpose

The Treadmill CLI (`treadmill`) is the operator's interface to the Treadmill API (per ADR-0010). It wraps HTTP calls to the API and presents results via Rich tables for human consumption.

## Key surfaces

- `treadmill_cli/cli.py` — main entrypoint; registers all command groups and defines plan/task/workflow/role subcommands inline.
- `treadmill_cli/api_client.py` — thin `ApiClient` wrapper over `httpx`; all API calls go through `_request`. Covers plans, tasks, workflows, onboarding repos, and health endpoints.
- `treadmill_cli/commands/learnings.py` — `treadmill learnings crystallize` (ADR-0034).
- `treadmill_cli/commands/schedules.py` — `treadmill schedules list|create|pause|resume|delete` (ADR-0035).
- `treadmill_cli/commands/onboarding.py` — `treadmill onboarding update <repo>` for managing per-repo worker deps (ADR-0059 Step 5): `--worker-deps-python`, `--worker-deps-node`, `--binary name=URL=SHA256@TARGET`, `--clear-worker-deps`.
- `treadmill_cli/observe.py` — `treadmill observe` read-only Grafana access layer (ADR-0020).
- `treadmill_cli/config.py` — config loading from env vars (`TREADMILL_API_URL`, `TREADMILL_API_KEY`).

## Recent changes

- [#TBD](https://github.com/joeLepper/treadmill/pull/TBD) — ADR-0059 Step 5 (operator CLI for worker deps): new `commands/onboarding.py` adds `treadmill onboarding update <repo>` with `--worker-deps-python` / `--worker-deps-node` / `--binary name=URL=SHA256@TARGET` (repeatable, additive, deduplicated) and `--clear-worker-deps` (mutually exclusive with dep flags). `ApiClient` gains `get_repo_config` (GET `/api/v1/onboarding/repos/{repo}`) and `upsert_repo_config` (POST `/api/v1/onboarding/repos`). 404 on GET surfaces "run `treadmill onboarding add` first"; 422 on POST surfaces the validation error with exit 1. CLI registered in `cli.py`.
