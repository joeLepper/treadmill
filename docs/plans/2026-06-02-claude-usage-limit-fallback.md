---
auto_merge: true
status: completed
---

# Plan: Usage-limit fallback for Claude account routing

- **Status:** completed
- **Date:** 2026-06-02
- **Related ADRs:** ADR-0066 (this), ADR-0055 (per-account Claude credentials)

## Goal

Let a repo name a **primary** Claude account and an explicit **fallback** account, and
have a worker fall back to the fallback — once, per failing subprocess — only when the
primary hits a usage limit. Per ADR-0066: server-side resolution (the worker never picks
an account), per-step retry-primary-first, best-effort fallback (a misconfigured fallback
never breaks a working primary).

## Success criteria

- `RepoConfig` round-trips a new `claude_account_fallback` field; the `repo_configs` table
  has a nullable column for it; existing repos (no fallback) behave exactly as before.
- `POST /api/v1/claude/credentials` returns an optional `fallback` block when the repo
  configures one; a misconfigured fallback yields a primary-only 200 (never an error).
- A Claude Code subprocess that exits non-zero with a usage-limit signature, when a
  fallback credential is present, re-runs **once** with the fallback token; a non-limit
  failure or an absent fallback does **not** retry. A WARNING + OTel counter record the
  fallback.

## Constraints / scope

### In scope
The routing capability only: schema + migration, the resolver's optional fallback block,
and the worker's detect-and-retry. Generic account names throughout (`primary`/`fallback`,
fixtures like `acme/widget`) — no real repo or account names in code or docs.

### Out of scope
Sticky/cooldown fallback; per-account token-usage attribution; a typed dashboard event
(log + counter only); fallback chains deeper than one. Operator wiring (mint tokens, CDK,
secrets, `repo_configs` bindings) is the operator checklist below — not code, not committed.

### Budget
Three sequential tasks, `auto_merge: true`. The changes are additive and opt-in (nullable
column, optional response field, fallback fires only when an operator configures one), and the
`depends_on` chain already serializes the merges, so auto-merge after green CI + cooling-off is
appropriate; no manual merge bottleneck.

## sequence_of_work

```yaml
sequence_of_work:
  - id: claude-fallback-schema
    title: Add RepoConfig.claude_account_fallback (dataclass + ORM + store + migration)
    workflow: wf-author
    intent: |
      Add an optional per-repo fallback Claude account, mirroring the existing
      ``claude_account`` field in every place that field appears (ADR-0066, amends
      ADR-0055). Pure additive change; ``None`` everywhere is the legacy behaviour.

      Read first — ``claude_account`` is the exact template; copy its treatment:
        * ``services/api/treadmill_api/repo_config.py`` — ``RepoConfig`` dataclass
          (``claude_account: str | None = None`` at ~line 34), ``parse_repo_config``
          (~line 65), ``to_dict`` (~line 77).
        * ``services/api/treadmill_api/models/onboarding.py`` — ``RepoConfigRow``
          ``claude_account`` mapped_column (~line 127).
        * ``services/api/treadmill_api/onboarding_store.py`` — ``upsert_repo_config``
          insert branch (~line 56) and update branch (~line 67), and
          ``get_repo_config`` read (~line 125).

      (1) ``RepoConfig``: add ``claude_account_fallback: str | None = None`` directly
      after ``claude_account``. Thread it through ``parse_repo_config``
      (``data.get("claude_account_fallback")``) and ``to_dict``.

      (2) ``RepoConfigRow``: add a ``claude_account_fallback`` mapped_column,
      ``String(64)``, nullable, mirroring ``claude_account``.

      (3) ``onboarding_store``: set ``claude_account_fallback=config.claude_account_fallback``
      in the insert kwargs, ``existing.claude_account_fallback = config.claude_account_fallback``
      in the update branch, and ``claude_account_fallback=row.claude_account_fallback`` in the
      ``get_repo_config`` ``RepoConfig(...)`` return.

      (4) MIGRATION: new file under ``services/api/alembic/versions/`` mirroring
      ``20260526_1500_repo_configs_claude_account.py`` exactly — ``op.add_column`` a
      nullable ``sa.String(length=64)`` named ``claude_account_fallback`` on ``repo_configs``;
      ``downgrade`` drops it. Set ``down_revision`` to the CURRENT head — run
      ``cd services/api && uv run alembic heads`` and use that revision id; do NOT guess
      the predecessor.

      (5) TESTS — ``services/api/tests/test_repo_config.py``: extend the round-trip /
      defaults tests to cover ``claude_account_fallback`` (defaults to ``None``; parses and
      ``to_dict`` round-trips a set value). ``services/api/tests/test_onboarding_store.py``
      has a pure (no-DB) attribute test plus Postgres-gated tests — add the new field to the
      pure attribute assertion only; the DB-gated tests skip in the worker sandbox, do not
      try to make them run.

      (6) DOCS (ADR-0030): ``services/api/AGENT.md`` — note the new RepoConfig field under
      the relevant surface and cite ADR-0066.
    scope:
      files:
        - services/api/treadmill_api/repo_config.py
        - services/api/treadmill_api/models/onboarding.py
        - services/api/treadmill_api/onboarding_store.py
        - services/api/alembic/versions/20260602_1200_repo_configs_claude_account_fallback.py
        - services/api/tests/test_repo_config.py
        - services/api/tests/test_onboarding_store.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/routers/claude_credentials.py
        - workers/agent/
    validation:
      - kind: deterministic
        description: |
          repo_config parser/serializer round-trips the new field. Pure unit test,
          no database (test_onboarding_store needs Postgres and is intentionally not gated).
        script: |
          cd services/api && uv run pytest tests/test_repo_config.py -q

  - id: claude-fallback-resolver
    title: Return optional fallback credential from the claude/credentials resolver
    workflow: wf-author
    depends_on:
      - task.claude-fallback-schema.pr_merged
    intent: |
      Extend the Claude credential resolver (ADR-0066) so that when a repo configures a
      fallback account, the response carries a second credential. The primary path is
      UNCHANGED — same 404/502/503 semantics. Fallback resolution is BEST-EFFORT: any
      fallback-side failure logs a warning and returns the primary-only response; it must
      never turn a working primary into an error.

      Read first: ``services/api/treadmill_api/routers/claude_credentials.py`` —
      ``ClaudeCredentialsResponse`` (~line 48), the resolver ``fetch_claude_credentials``
      (~line 88), and how it resolves ``account_name`` / fetches the secret (~line 105-152).
      Tests: ``services/api/tests/test_claude_credentials_router.py`` — fully stubbed
      (``_StubSession``, ``_FakeStore``, ``_FakeSecretsManager``, monkeypatched
      ``OnboardingStore`` + ``_make_secrets_client``); copy its harness for the new cases.

      (1) Add a nested model ``ClaudeFallbackCredential(BaseModel)`` with ``account: str``,
      ``type: Literal["oauth","api_key"]``, ``token: str``; add
      ``fallback: ClaudeFallbackCredential | None = None`` to ``ClaudeCredentialsResponse``.

      (2) In the resolver, after the primary credential is built (unchanged), read
      ``cfg.claude_account_fallback`` (``cfg`` is the already-fetched ``RepoConfig``; ``None``
      when the repo isn't onboarded). If it is set:
        - look it up in the same parsed ``accounts`` map; if absent → log a warning and
          leave ``fallback=None`` (do NOT 404);
        - else fetch its secret with the SAME ``sm`` client + the SAME guards (SM exception
          or empty ``SecretString``) but on failure → log a warning and leave ``fallback=None``
          (do NOT 502);
        - on success attach ``ClaudeFallbackCredential(account=<name>, type=account.type,
          token=<secret>)``.
      Never log token values; the account name + type is the safe routing identifier.

      (3) TESTS — add to ``test_claude_credentials_router.py``: (a) repo with a valid
      fallback → response ``fallback`` populated with the right account/type/token; (b) repo
      with no fallback → ``fallback is None``; (c) fallback name not in the accounts map →
      primary 200 + ``fallback is None``; (d) fallback secret fetch raises → primary 200 +
      ``fallback is None``. Use a ``_FakeStore`` that returns a ``RepoConfig`` carrying
      ``claude_account_fallback`` and a ``_FakeSecretsManager`` keyed by secret name.

      (4) DOCS (ADR-0030): ``services/api/AGENT.md`` — document the new optional ``fallback``
      block on the credentials response and cite ADR-0066.
    scope:
      files:
        - services/api/treadmill_api/routers/claude_credentials.py
        - services/api/tests/test_claude_credentials_router.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/repo_config.py
        - workers/agent/
    validation:
      - kind: deterministic
        description: |
          The resolver returns the fallback block when configured and degrades to
          primary-only (never errors) when the fallback is misconfigured. Fully stubbed
          test module — no database, no real boto3.
        script: |
          cd services/api && uv run pytest tests/test_claude_credentials_router.py -q

  - id: claude-fallback-worker-retry
    title: Worker detects usage limit and retries the failing Claude call on the fallback
    workflow: wf-author
    depends_on:
      - task.claude-fallback-resolver.pr_merged
    intent: |
      Make the worker carry the optional fallback credential and, when a Claude Code
      subprocess fails with a usage-limit signature, re-run THAT subprocess once with the
      fallback token (ADR-0066). No runner change is needed — the runner already sets the
      resolved creds around ``_execute`` via ``set_claude_creds``; the retry lives inside the
      ``claude_code`` Popen helpers, which read ``_CURRENT_CREDS``.

      Read first:
        * ``workers/agent/treadmill_agent/startup_auth.py`` — ``ClaudeCreds`` dataclass
          (~line 46) and ``fetch_claude_credentials`` (~line 61), which parses the
          credentials response.
        * ``workers/agent/treadmill_agent/claude_code.py`` — ``build_claude_env``
          (~line 88), ``run_claude`` (~line 126), ``run_claude_code`` (~line 203); both
          share the Popen + two ``_pump_stream`` threads + ``proc.wait(timeout)`` structure.
        * ``workers/agent/treadmill_agent/observability.py`` — ``record_token_usage``
          (~line 148) is the lazy-meter pattern to mirror.

      (1) ``ClaudeCreds``: add ``fallback: "ClaudeCreds | None" = None``. In
      ``fetch_claude_credentials``, when the response has a non-null ``fallback`` object,
      build a nested ``ClaudeCreds(account, type, token, fallback=None)`` and attach it.
      Log only ``account``/``type`` (and that a fallback is present) — never tokens.

      (2) ``claude_code.py``:
        - Add ``looks_like_usage_limit(stdout: str, stderr: str) -> bool`` — a CONSERVATIVE
          case-insensitive match over the combined text for usage-limit signatures
          (``usage limit``, ``rate limit`` / ``rate_limit_error``, ``overloaded``, standalone
          ``429``, quota / "limit reached" / "resets at" markers). IMPORTANT: confirm the
          actual strings Claude Code ``@anthropic-ai/claude-code@2.1.138`` emits for an
          exhausted OAuth subscription in ``--print --output-format json`` mode (it surfaces
          the message in the JSON ``result``/``is_error`` and/or on stderr) and pin them as
          test samples; keep the matcher narrow enough not to fire on ordinary failures.
        - Factor the shared subprocess body of ``run_claude`` and ``run_claude_code`` into a
          helper, e.g. ``_run_claude_subprocess(cmd, *, cwd=None, timeout, log_extra) ->
          tuple[int, str, str]`` (returncode, stdout_text, stderr_text), preserving the
          ``bufsize=1`` + dual ``_pump_stream`` threads + ``TimeoutExpired`` kill/join
          behaviour. Both call sites build their ``cmd`` then call the helper.
        - Wrap the helper with the fallback retry: read ``creds = _CURRENT_CREDS.get()``;
          first run uses ``build_claude_env(os.environ, creds)``. If ``returncode != 0`` AND
          ``looks_like_usage_limit(stdout, stderr)`` AND ``creds is not None and
          creds.fallback is not None`` → rebuild env via ``build_claude_env(os.environ,
          creds.fallback)``, re-run the SAME cmd ONCE, and use the second run's
          ``(returncode, stdout, stderr)`` as the result. On fallback, emit a WARNING
          ``"claude credential fallback fired"`` (from ``creds.account`` to
          ``creds.fallback.account``, no tokens) and call ``observability.record_claude_fallback``.
          The existing non-zero-exit ``CodeAuthorError`` raise and the JSON parse in
          ``run_claude_code`` operate on the FINAL run's output.
        - ``run_claude``'s timeout-path message and ``run_claude_code``'s JSON/token-usage
          handling stay as they are, just fed by the helper's final output.

      (3) ``observability.py``: add ``record_claude_fallback(*, from_account: str,
      to_account: str, repo: str = "", role: str = "")`` that lazily creates and increments
      a counter ``treadmill.claude.fallback`` with those attributes, mirroring
      ``record_token_usage`` (no-op when OTel unconfigured).

      (4) TESTS — use the ``CLAUDE_BINARY`` env override (``_find_binary``) to point at a
      stub script (write it under a tmp_path) that exits non-zero and prints a usage-limit
      message, plus one that exits non-zero with an unrelated error, plus a success stub.
      In ``workers/agent/tests/test_claude_code.py`` / ``test_claude_code_env.py``:
        - ``looks_like_usage_limit`` returns True on the captured sample strings, False on
          ordinary stderr.
        - With a usage-limit stub AND ``_CURRENT_CREDS`` set to creds-with-fallback, the
          subprocess runs TWICE and the second invocation's env carries the fallback token
          (have the stub echo which token env it saw into a file, or assert call count +
          env). ``record_claude_fallback`` is invoked.
        - Usage-limit stub but creds WITHOUT fallback → runs once, raises ``CodeAuthorError``.
        - Non-usage-limit failure with a fallback present → runs once, raises (no retry).
        Set/reset ``_CURRENT_CREDS`` with ``set_claude_creds``/``reset_claude_creds`` and
        keep tests hermetic (no network, stub binary only).

      (5) DOCS (ADR-0030): ``workers/agent/AGENT.md`` — document the usage-limit fallback
      retry in the Claude Code wrapper surface and cite ADR-0066.
    scope:
      files:
        - workers/agent/treadmill_agent/startup_auth.py
        - workers/agent/treadmill_agent/claude_code.py
        - workers/agent/treadmill_agent/observability.py
        - workers/agent/tests/test_claude_code.py
        - workers/agent/tests/test_claude_code_env.py
        - workers/agent/AGENT.md
      out_of_scope:
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_claude_code_real_binary.py
    validation:
      - kind: deterministic
        description: |
          Usage-limit detection + one-shot fallback retry behave per ADR-0066, exercised
          with a stub CLAUDE_BINARY (no network, no real Claude). Hermetic unit tests.
        script: |
          cd workers/agent && uv run pytest tests/test_claude_code.py tests/test_claude_code_env.py -q
```

## Diagram

See ADR-0066's sequence diagram for the resolve → run → detect → retry flow.

## Risks / unknowns

- **Usage-limit detection brittleness (load-bearing).** The exact strings Claude Code
  `2.1.138` emits for an exhausted OAuth subscription must be confirmed and pinned as test
  samples. Too broad → spurious fallback (silently spends the secondary); too narrow →
  misses the case. Mitigation: conservative signature set + sample-pinned tests; revisit on
  every Claude Code version bump. We'll abort the worker task and reassess if the real
  output can't be reproduced/identified.
- **Migration is verified at deploy, not in the gate** (the gate is the pure parser test;
  `test_onboarding_store` needs Postgres). Mitigation: the migration mirrors the proven
  `20260526_1500` file exactly and sets `down_revision` from live `alembic heads`.

## Operator checklist (Joe's hands — needs your credentials; NOT part of the PRs)

Run after all three PRs merge and the API is redeployed with the migration applied.
Substitute your real repo slugs / account names locally — keep them out of committed docs.

1. Mint a `<primary>` OAuth token (`claude setup-token` on the primary subscription) and a
   `<fallback>` token if not already minted.
2. `cd infra && cdk synth -c claude_accounts=<primary>,<fallback>` then deploy — adds the
   two empty secrets + extends the API IAM `GetSecretValue` ARNs (see `infra/AGENT.md`).
3. `aws secretsmanager put-secret-value` the two tokens into their secrets.
4. Set the API deployment env `CLAUDE_ACCOUNTS_JSON` with both accounts
   (`{"<primary>":{"type":"oauth","secret_name":"..."},"<fallback>":{...}}`); optionally
   `CLAUDE_DEFAULT_ACCOUNT`.
5. Set `repo_configs` for both target repos: `claude_account=<primary>`,
   `claude_account_fallback=<fallback>` (onboarding upsert or one-line SQL — I'll draft it).
6. Redeploy the API; smoke a step on each repo and confirm `account=<primary>` in the worker
   logs. To exercise fallback, point a step at a limit-exhausted primary token and confirm
   the `claude credential fallback fired` WARNING + the step still completing.

## Post-mortem

Completed 2026-06-04. All three tasks merged (PRs #133 A, #135 B, #139 C); the agent
image rebuilt with C; ramjac wired `claude_account=hearth` /
`claude_account_fallback=personal` and verified end-to-end (resolver returns both
credentials). The hearth-as-primary half shipped earlier and independently of the
fallback code — ADR-0055 single-account routing was already live, so it needed only the
CDK secret + token + YAML + one `repo_configs` UPDATE.

**What worked.** The pattern (ADR → submittable plan → dispatch → auto-merge) held; the
architect-as-recoverer (ADR-0048) fixed B's stuck test without intervention, so the
prepared cancel+re-dispatch tripwire never fired.

**What surprised us.** Almost none of the wall-clock was the code — it was infra. Three
egress-proxy / stale-process failures wedged the fleet mid-plan: bare `github.com` not in
the proxy allowlist (clone 403), package registries scoped to install-phase-only so every
`uv run pytest` gate hit a tunnel error (fixed in #132), and the running autoscaler
serving pre-merge code because host processes pin modules at import time. The last became
learning `2026-06-04-host-processes-pin-code-at-import-time` and a sibling session's
ADR-0069 (self-heal managed host processes on source change).

**What should become an ADR / learning / rule.** Two terminal-gating defects, both
captured as learnings: an architect `accept-as-is` on an *unmerged* PR terminalized task A
before merge (stranded the PR — we manual-merged), and the *inverse* — a merged/terminal
task still accepted post-merge review/feedback runs that looped on the doc-currency judge
and **starved task B of workers for ~53 minutes**. The remediation (stuck-task sweep flags
`terminal status + open PR` or `terminal status + active runs`) is worth its own task.

**What this teaches future plans.** The plan was sound; the cost lived in the platform the
gates run on. Verify a host-process fix from an artifact the *process* emits, not the
source tree — file-fresh is not process-fresh.
