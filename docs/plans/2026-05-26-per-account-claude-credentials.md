# Plan: Per-account Claude credential routing for workers

- **Status:** drafting
- **Date:** 2026-05-26
- **Related ADRs:** ADR-0055

## Goal

Ship per-account Claude credential routing end-to-end so a deployment can bill Claude work on different repos to different Claude accounts, with a per-account credential-type knob (`oauth` now, `api_key` later) ready for a future workers-on-API-key migration.

## Success criteria

- A repo with `claude_account: <X>` configured causes the worker to launch Claude Code with the env var derived from account X's secret, and no inherited file-mount credentials in the child process's effective auth.
- A repo with no `claude_account` (or null) uses the deployment's `claude_default_account`. When no accounts are configured at all, the existing `CLAUDE_CREDENTIALS_PATH` mount continues to work unchanged.
- Account resolution failure (unknown account, missing secret, type mismatch) fails the step with a clear error — never falls through to a different account.
- Live two-account smoke: a task on a default-account repo and a task on a secondary-account repo each open a PR; usage shows on the correct account for each.

## Constraints / scope

### In scope
- API: new `POST /api/v1/claude/credentials` endpoint + settings schema for the account map + tests.
- DB: `RepoConfig.claude_account` (nullable) — Alembic migration + onboarding accepts it.
- Worker: per-step credential fetch slotted next to the App-token re-mint; subprocess env injection at both `claude_code.py` `Popen` sites.
- CDK + local-adapter: parameterized secret resources per configured account; deployment YAML carries `claude_accounts` + `claude_default_account`.

### Out of scope
- Interactive multi-account wrapper (`CLAUDE_CONFIG_DIR`) — deferred per operator preference.
- Bedrock / Vertex / Foundry credential routing — separate ADR if/when needed.
- Migrating any specific account from OAuth → API key (schema supports it; no account is being migrated here).
- Per-account spend observability — separate.

### Budget
Two focused dev days. If the API endpoint or the worker env-injection runs over, abort and reconsider.

## Sequence of work

1. **Schema + onboarding.** Add `claude_account: str | None` to `repo_configs` (Alembic migration), `RepoConfig` dataclass, `OnboardingStore.upsert_repo_config`/`get_repo_config`, and the onboarding router request/response. Scope-in: `services/api/AGENT.md` (Recent changes + the onboarding surface line) and the existing `tests/test_onboarding_store.py` + `tests/test_onboarding_router.py` (both touch the `RepoConfig` shape and will trip on a new field without an update).
2. **API endpoint + settings.** `POST /api/v1/claude/credentials {repo}` → `{account, type, token}`. Resolves via `OnboardingStore.get_repo_config` + `Settings.claude_accounts` map (parsed from `CLAUDE_ACCOUNTS_JSON` env) + `claude_default_account`. 503 when no accounts configured; 404 when the resolved account isn't in the map; 400 on a bad `type`. Unit tests with a fake `SecretsManagerClient`. Scope-in: `services/api/AGENT.md` (Key surfaces + Recent changes) and `tests/test_config.py` if it asserts known settings fields.
3. **Worker fetch + subprocess env injection.** Add `fetch_claude_credentials(repo)` to `startup_auth` mirroring `bootstrap_github_auth_via_app`. In `runner._handle_step`, after the App re-mint, fetch the Claude credential and thread it into the Claude launch path. `claude_code.py`: accept an explicit `env` mapping at both `Popen` call sites so callers control which credential env var is set and clear `CLAUDE_CREDENTIALS_PATH` from the child env when a token is supplied. Scope-in: `workers/agent/AGENT.md` (Recent changes), and the existing `tests/test_runner.py`, `tests/test_runner_dispositions.py`, `tests/test_startup_auth.py` — all reference the launch path or the re-mint and will tip on a new arg without an update. Validation is `pytest`-primary across these test files; no exact-string greps of call signatures.
4. **CDK + local-adapter operator surface.** `constructs/secrets.py` accepts a list of account-name strings via CDK context and creates one `Secret` per account (named `<prefix>/claude-account-<name>`), granting `api_user` read. `deployment_config.py` + `runtime.py`: load `claude_accounts` map from operator YAML and inject as `CLAUDE_ACCOUNTS_JSON` + `CLAUDE_DEFAULT_ACCOUNT` into the API container. Scope-in: `infra/AGENT.md`, `tools/local-adapter/AGENT.md`, `tests/test_cloud_lite_stack.py` (expected resource counts), `tests/test_deployment_config.py`, `tests/test_runtime.py`.
5. **Live smoke.** Operator-side: `claude setup-token` for each account; populate the two secrets; onboard one repo with `claude_account: <secondary>`; run a trivial PR task on each repo; verify Claude usage shows on the correct account for each.

## Diagram

See ADR-0055 — the per-step credential-fetch sequence.

## Risks / unknowns

- **Silent cross-account leakage** if Claude Code inherits a stale env var or the bind-mounted file when an account is configured: the worker explicitly builds the child env and asserts only one of `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` is set, with `CLAUDE_CREDENTIALS_PATH` removed. Abort if we can't make that assertion clean.
- **Bearer-token leakage in logs**: tests assert the credential string never appears in captured subprocess stdout/stderr, and we grep error paths for token-in-message.
- **`claude setup-token` fails for the operator**: external dependency. Document the manual step + the alternative (`CLAUDE_CONFIG_DIR=<dir> claude` interactive re-login, then `setup-token`).

## Decisions captured during execution
*(populated as we go)*

## Post-mortem
*(filled on completion)*
