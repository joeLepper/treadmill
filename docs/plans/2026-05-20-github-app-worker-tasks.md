---
auto_merge: true
status: active
---

# Plan: GitHub App migration — worker-dispatchable units

- **Status:** drafting
- **Date:** 2026-05-20
- **Related ADRs:** ADR-0049
- **Related plans:** 2026-05-20-github-app-identity-migration (the full migration)

## Goal

Dispatch the self-contained, additive pieces of the GitHub App migration
(ADR-0049 phases 4, 6, 7) to Treadmill's own workers. Each task adds a new
module/function + tests **without rewiring any live path** — the cutover wiring
(call sites, poller, PAT decommission) and the credential-sensitive phases
(5, 8) stay hand-driven. This both ships the pieces and stress-tests the
hardened pipeline on real Treadmill-built-by-Treadmill work.

## Success criteria

Each task lands a PR (manual-merge; `auto_merge: false`) that adds the named
module + a passing named test, touches only its scoped files, and leaves the
existing PAT / webhook paths in force.

## Constraints / scope

### In scope
The three additive units below (phase 4 auth provider, phase 6 dual-secret
verify, phase 7 installation registry), each building on the merged phase-3
`treadmill_api/github_app.py`.

### Out of scope
Rewiring live call sites (the API merge path, the webhook poller), Secrets
Manager/private-key resolution plumbing, phase 5 (worker auth), phase 8 (PAT
decommission). Those follow by hand once these units land.

## sequence_of_work

```yaml
sequence_of_work:
  - id: github-app-auth-provider
    title: GitHub App auth provider (PAT-or-installation-token, config-selected)
    workflow: wf-author
    intent: |
      Create a NEW module ``services/api/treadmill_api/github_auth.py`` that
      abstracts "give me a GitHub token for this repo", selecting between the
      legacy PAT and the GitHub App per config. This is ADDITIVE — do NOT edit
      ``app.py`` or ``coordination/triggers.py`` (wiring the call sites is a
      separate hand-driven step).

      Build on the already-merged ``treadmill_api/github_app.py`` (phase 3),
      which provides ``InstallationTokenCache(client, *, app_id,
      private_key_pem)`` with an async ``token_for_repo(repo) -> str``.

      Define in the new module:
        - A class ``GitHubAuthProvider`` with an async method
          ``token_for_repo(self, repo: str) -> str``.
        - A factory ``build_github_auth_provider(settings, http_client)
          -> GitHubAuthProvider``:
            * When BOTH ``settings.github_app_id`` and
              ``settings.github_app_private_key`` are set (truthy), return a
              provider whose ``token_for_repo`` delegates to an
              ``InstallationTokenCache`` built from those values + the
              ``http_client`` (App path).
            * Otherwise return a provider whose ``token_for_repo`` returns
              ``settings.github_token`` for any repo (legacy PAT path).
        - ``settings`` is the ``treadmill_api.config.Settings`` instance;
          the relevant fields (``github_app_id``, ``github_app_private_key``,
          ``github_token``) already exist.

      Keep it small; do not fetch from Secrets Manager here (the private key
      is taken from ``settings.github_app_private_key`` already-resolved).

      Then create a NEW test file
      ``services/api/tests/test_github_auth.py`` that:
        - builds a fake/minimal Settings-like object (or uses
          ``treadmill_api.config.Settings`` with values set) for two cases;
        - PAT case (no app id / no private key): ``token_for_repo("a/b")``
          returns the configured ``github_token``;
        - App case (app id + a dummy private key set): patches
          ``treadmill_api.github_auth.InstallationTokenCache`` (or the
          cache's ``token_for_repo``) so no network happens, and asserts
          ``token_for_repo("a/b")`` returns the installation token from the
          cache, not the PAT.
        - Use ``pytest.mark.asyncio`` for the async calls; mock with
          ``unittest.mock``. No network, no real key needed for the App case
          (patch the cache).
    scope:
      files:
        - services/api/treadmill_api/github_auth.py
        - services/api/tests/test_github_auth.py
      out_of_scope:
        - services/api/treadmill_api/app.py
        - services/api/treadmill_api/coordination/triggers.py
    validation:
      - kind: deterministic
        description: |
          The new module exists and the new test passes; no live call sites
          touched.
        script: |
          cd services/api \
            && grep -q "def build_github_auth_provider" treadmill_api/github_auth.py \
            && uv run pytest tests/test_github_auth.py -q

  - id: webhook-dual-secret-verify
    title: Webhook signature verification accepts either secret (App-secret cutover prep)
    workflow: wf-author
    intent: |
      Add support for verifying a GitHub webhook signature against EITHER of
      two secrets (the legacy webhook secret OR the GitHub App's webhook
      secret), so a later cutover can accept both during the transition. This
      is ADDITIVE — do NOT change the webhook poller or any route to call the
      new function (that wiring is hand-driven later).

      In ``services/api/treadmill_api/webhooks/signatures.py`` there is an
      existing ``verify_github_signature(secret, body, signature_header)
      -> None`` that returns None on success and raises
      ``SignatureMissingError`` / ``InvalidSignatureError`` on failure
      (read the file; match its real behavior).

      Add a new function in the same module:
        ``verify_github_signature_any(secrets: list[str | None], body: bytes,
        signature_header: str | None) -> None`` that returns None if the
        existing ``verify_github_signature`` succeeds for ANY non-empty secret
        in ``secrets`` (catch its exceptions per-secret and try the next),
        and re-raises an ``InvalidSignatureError`` if none match. If
        ``secrets`` contains only ``None``/empty entries, treat it as the
        dev-mode skip (return None), matching the single-secret behavior.

      Also add two config fields to ``services/api/treadmill_api/config.py``
      mirroring the existing ``github_webhook_secret`` /
      ``github_webhook_secret_name`` pair:
        - ``github_app_webhook_secret`` (alias ``GITHUB_APP_WEBHOOK_SECRET``)
        - ``github_app_webhook_secret_name`` (alias
          ``GITHUB_APP_WEBHOOK_SECRET_NAME``)
        both ``str | None`` defaulting to ``None``.

      Then create a NEW test file
      ``services/api/tests/test_webhook_dual_secret.py`` that signs a body
      with HMAC-SHA256 (``"sha256=" + hmac.new(secret, body,
      sha256).hexdigest()``) and asserts:
        - ``verify_github_signature_any(["A"], body, sig_A)`` returns None;
        - ``verify_github_signature_any(["B", "A"], body, sig_A)`` returns
          None (second secret matches);
        - ``verify_github_signature_any(["B"], body, sig_A)`` raises
          ``InvalidSignatureError``;
        - ``verify_github_signature_any([None, ""], body, None)`` returns
          None (dev-mode skip).
    scope:
      files:
        - services/api/treadmill_api/webhooks/signatures.py
        - services/api/treadmill_api/config.py
        - services/api/tests/test_webhook_dual_secret.py
      out_of_scope:
        - services/api/treadmill_api/webhooks/pending_events.py
    validation:
      - kind: deterministic
        description: |
          The new verifier exists and the new test passes.
        script: |
          cd services/api \
            && grep -q "def verify_github_signature_any" treadmill_api/webhooks/signatures.py \
            && uv run pytest tests/test_webhook_dual_secret.py -q

  - id: github-installation-registry
    title: Onboarded-repo installation registry (multi-org on-ramp)
    workflow: wf-author
    intent: |
      Create a NEW module
      ``services/api/treadmill_api/github_installations.py`` with an in-memory
      registry of which repos Treadmill is onboarded onto and their GitHub App
      installation ids. This is the multi-org on-ramp concept (distinct from
      phase-3's token cache: the registry answers "is this repo onboarded, and
      what is its installation id"). Persistence to a DB is a deliberate
      follow-up — keep it in-memory with a clear TODO. ADDITIVE — add no
      router/endpoint and touch no other module.

      Build on ``treadmill_api/github_app.py`` (merged), which provides
      ``async resolve_installation_id(client, *, app_id, private_key_pem,
      repo) -> int``.

      Define a class ``InstallationRegistry``:
        - ``record(self, repo: str, installation_id: int) -> None`` — mark a
          repo onboarded with its installation id.
        - ``is_onboarded(self, repo: str) -> bool``.
        - ``known(self) -> dict[str, int]`` — copy of repo→installation_id.
        - ``async resolve(self, client, *, app_id, private_key_pem, repo)
          -> int`` — return the recorded installation id if present; else call
          ``resolve_installation_id``, record it, and return it (cache-first;
          one resolve per repo).

      Then create a NEW test file
      ``services/api/tests/test_github_installations.py`` that:
        - asserts ``record`` + ``is_onboarded`` + ``known`` behave;
        - asserts ``resolve`` calls the underlying
          ``resolve_installation_id`` only once across two calls for the same
          repo (patch
          ``treadmill_api.github_installations.resolve_installation_id`` with
          an ``unittest.mock.AsyncMock`` returning a fixed id, assert
          ``call_count == 1`` after two ``resolve`` calls);
        - uses ``pytest.mark.asyncio``; no network.
    scope:
      files:
        - services/api/treadmill_api/github_installations.py
        - services/api/tests/test_github_installations.py
    validation:
      - kind: deterministic
        description: |
          The registry module exists and the new test passes.
        script: |
          cd services/api \
            && grep -q "class InstallationRegistry" treadmill_api/github_installations.py \
            && uv run pytest tests/test_github_installations.py -q
```
