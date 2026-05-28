---
auto_merge: false
---

# Plan: ADR-0060 — Sidecar HTTPS proxy for worker egress scoping

- **Status:** drafting
- **Date:** 2026-05-28
- **Related ADRs:** ADR-0060, ADR-0059 (worker_deps registration —
  the consumer of the install-phase allowlist), ADR-0055 (per-account
  Claude credentials — the sibling secrets-channel shape)
- **Supersedes:** none
- **Depends on:** ADR-0059 Steps 1, 2, 4, 5 (already shipped)

## Goal

Ship the four-piece egress-scoping surface for worker network access:
a sidecar HTTPS proxy, autoscaler wiring that puts workers on the
proxy's network with no other route, runner-side credential plumbing
that limits install-phase egress to materialize only, and an
integration smoke that proves the allow / deny paths fire as designed.

Closes ADR-0059's Step 3 (the determinism complement that makes
worker_deps registration the *only* channel for a dep to reach the
worker). Unblocks the 5 wedged RAMJAC tasks once the operator registers
`aws-cdk-lib==X.Y.Z` against the now-allowlisted PyPI route.

`auto_merge: false` — concurrent operator on the RAMJAC unblock track;
manual merge per PR keeps the orchestration clean.

## Success criteria

- A `treadmill-egress-proxy` service exists at `services/egress-proxy/`
  (uv workspace member) that:
  - Reads per-worker allowlist config from a directory the autoscaler
    writes to (keyed by worker IP); reloads on `mtime` change.
  - Speaks HTTP `CONNECT`; for each request, decides allow / deny by
    hostname against the `always_allowed` list, and additionally
    against `install_allowed` when the request carries a matching
    install-phase credential.
  - Logs every decision in a stable structured form (hostname,
    phase, decision).
- The autoscaler (`tools/local-adapter/` for dev-local) creates a
  docker network with no external gateway, spawns the proxy on it,
  spawns each worker on it with the proxy as the only routable
  external host, mints a per-worker install credential, and writes
  the per-worker allowlist config (always-allowed defaults + repo's
  `WorkerDeps.binaries[].download_url` hostnames into install-phase).
- The worker runtime:
  - Entrypoint sets `HTTPS_PROXY=http://treadmill-egress-proxy:3128`
    (uncredentialed) — this is the task-phase default.
  - `repo_deps.materialize()` constructs a credentialed proxy URL
    from the env's install-credential and overrides `HTTPS_PROXY` in
    the subprocess env it passes to `pip` / `npm` / `urllib`.
  - `validation_runtime.run_deterministic` and the `claude`
    invocation do NOT see the credential; their subprocess env
    carries only the uncredentialed proxy URL.
- An integration smoke test exercises the end-to-end path: proxy
  running on a test socket, materialize() request to an allowlisted
  registry passes, the same request from a task-phase subprocess
  fails proxy-denied, a request to an unrelated hostname fails in
  both phases.
- AGENT.md updates land alongside each code change per ADR-0030 —
  `services/egress-proxy/AGENT.md`, `tools/local-adapter/AGENT.md`,
  `workers/agent/AGENT.md`.

## Constraints / scope

### In scope

- The four implementation tasks below.
- AGENT.md updates per ADR-0030.
- Unit tests for each module with `subprocess.run` / `docker` / socket
  mocks; no real PyPI / npm / external network in tests.
- Integration smoke that uses a locally-spawned proxy fixture
  binding to an ephemeral port.

### Out of scope

- TLS interception / body inspection (ADR-0060 reserves as B-track).
- Audit-log shipping (the proxy writes to stdout; rotation lands
  later via journald or similar).
- AWS CodeArtifact allowlist entries (ADR-0060 future-extension hook;
  ships when the first CodeArtifact-using repo onboards).
- Production EC2 cdk infra changes — dev-local autoscaler is the
  first surface; the EC2 host wiring is a follow-up plan once the
  dev-local pattern stabilizes.
- Reversing ADR-0059 Step 2's worker-side materialization (still on
  the worker; this plan only adds a credential to its subprocess env).

### Budget

Four worker dispatches, one per task. If any task wedges at the
architect-amend cap, the wedge is investigated before the next task
ships (no parallel dispatches against this plan).

## Sequence of work

```yaml
sequence_of_work:
  - id: egress-proxy-service
    title: "ADR-0060 Step 3a — treadmill-egress-proxy service"
    workflow: wf-author
    intent: |
      STUDY: read these as shape references —
        - `services/api/treadmill_api/models/onboarding.py` for the
          Pydantic-with-strict-config style used elsewhere on the
          API side (`ConfigDict(extra="forbid")`).
        - `workers/agent/pyproject.toml` for the workspace-member
          shape (project.scripts, requires-python, dependencies
          declared minimal).

      BUILD: a new uv workspace member at `services/egress-proxy/`:
        - `pyproject.toml` declaring `treadmill-egress-proxy`,
          requires-python >=3.12, deps minimal (stdlib asyncio plus
          one of: aiohttp, or pure stdlib `http.server` style — pick
          the simpler shape that gets a CONNECT proxy + per-request
          allowlist decision + structured log).
        - `treadmill_egress_proxy/config.py` with a Pydantic
          `WorkerAllowlist` model containing:
            - `always_allowed: list[str]` — hostnames reachable in
              both phases (Anthropic, GitHub, Treadmill API).
            - `install_allowed: list[str]` — additionally reachable
              when the request carries the install credential.
            - `install_credential_hash: str` — sha256 hex of the
              minted credential; the proxy hashes the
              Proxy-Authorization header's password and compares.
            - `worker_ip: str` — the docker-bridge IP this config
              applies to.
          A `ConfigStore` class loads `*.json` files from a
          configurable directory, keyed by `worker_ip`, with an
          mtime-cache so reloads only re-read when files change.
        - `treadmill_egress_proxy/proxy.py` implements the CONNECT
          proxy. For each incoming connection: parse the CONNECT
          target hostname, look up the source-IP's allowlist via
          ConfigStore, decide allow / deny per the rules above
          (always-allowed always; install-allowed only when
          Proxy-Authorization's password hashes to
          `install_credential_hash`). Log every decision as a single
          JSON line (timestamp, worker_ip, hostname, phase, decision,
          reason). On allow, tunnel the connection through to the
          upstream; on deny, return `HTTP/1.1 403 Forbidden` with a
          short body naming the rule that failed.
        - `treadmill_egress_proxy/__main__.py` entrypoint that reads
          `EGRESS_PROXY_CONFIG_DIR` + `EGRESS_PROXY_PORT` from the
          env and runs the proxy.
        - `tests/test_config.py` covering ConfigStore load + mtime
          reload + missing-IP behavior.
        - `tests/test_proxy.py` covering: always-allowed allow path;
          install-allowed deny without credential; install-allowed
          allow with correct credential; install-allowed deny with
          wrong credential; unknown-hostname deny in both phases.
          Use a synthetic upstream socket fixture (asyncio
          `start_server`) bound to localhost ephemeral port; no
          real external network.

      Also add `services/egress-proxy` to the root `pyproject.toml`
      `[tool.uv.workspace] members` list so the package installs
      into the workspace.

      Also write `services/egress-proxy/AGENT.md` describing the
      module's responsibilities, the config-file shape, and the
      stdout log line format. Reference ADR-0060.
    scope:
      files:
        - services/egress-proxy/pyproject.toml
        - services/egress-proxy/treadmill_egress_proxy/__init__.py
        - services/egress-proxy/treadmill_egress_proxy/__main__.py
        - services/egress-proxy/treadmill_egress_proxy/config.py
        - services/egress-proxy/treadmill_egress_proxy/proxy.py
        - services/egress-proxy/tests/__init__.py
        - services/egress-proxy/tests/test_config.py
        - services/egress-proxy/tests/test_proxy.py
        - services/egress-proxy/AGENT.md
        - pyproject.toml
      services_affected:
        - egress-proxy
      out_of_scope:
        - workers/agent/
        - tools/local-adapter/
    validation:
      - kind: deterministic
        description: |
          The new package's pytest suite passes against its own tests.
        script: |
          uv run --package treadmill-egress-proxy pytest services/egress-proxy/tests -q
      - kind: deterministic
        description: |
          The proxy module exposes a CONNECT handler and the config
          module exposes the WorkerAllowlist model + ConfigStore.
        script: |
          grep -lE "class WorkerAllowlist|class ConfigStore" services/egress-proxy/treadmill_egress_proxy/config.py
          grep -lE "CONNECT" services/egress-proxy/treadmill_egress_proxy/proxy.py
      - kind: deterministic
        description: |
          AGENT.md describes the module and references ADR-0060.
        script: |
          grep -lE "ADR-0060" services/egress-proxy/AGENT.md

  - id: autoscaler-proxy-wiring
    title: "ADR-0060 Step 3b — autoscaler spawns proxy + isolates workers"
    workflow: wf-author
    depends_on: [task.egress-proxy-service.pr_merged]
    intent: |
      STUDY:
        - `tools/local-adapter/treadmill_local/autoscaler.py` —
          the existing worker spawn site. Find where workers are
          launched via the docker client and what network config
          they currently inherit.
        - `services/egress-proxy/treadmill_egress_proxy/config.py`
          — the WorkerAllowlist shape the autoscaler must write.
        - `services/api/treadmill_api/models/onboarding.py` —
          `WorkerDeps.binaries[].download_url` is the source of the
          per-repo install-allowed hostnames.

      BUILD:
        - On autoscaler start: ensure a docker network named
          `treadmill-egress` exists with `internal=True` (no
          external gateway). Spawn the `treadmill-egress-proxy`
          container on this network with the config dir mounted
          read-only. Idempotent: if the container is already
          running, leave it.
        - On each worker spawn:
          - Mint a per-worker install credential (random 32-byte
            URL-safe token; sha256 it for the proxy config).
          - Resolve the repo's `WorkerDeps` (via the existing
            onboarding-fetch path); collect the binary download URLs
            and extract the unique hostnames for `install_allowed`.
            Always merge in PyPI / npm registry hostnames as the
            global install defaults: `pypi.org`,
            `files.pythonhosted.org`, `registry.npmjs.org`,
            `registry.yarnpkg.com`.
          - Build the `always_allowed` list from a static module
            constant: `api.anthropic.com`, `api.github.com`,
            `*.githubusercontent.com`, `*.github.com`, plus the
            per-deploy Treadmill API host (env var
            `TREADMILL_API_HOST`).
          - Spawn the worker container on the `treadmill-egress`
            network (no other network). Inject env:
            `HTTPS_PROXY=http://treadmill-egress-proxy:3128`,
            `HTTP_PROXY=http://treadmill-egress-proxy:3128`,
            `TREADMILL_INSTALL_PROXY_TOKEN=<the cred>`.
          - After the worker has an IP on the egress bridge, write
            `<config_dir>/<worker_ip>.json` with the
            `WorkerAllowlist` shape (the proxy picks it up via
            mtime watch). The worker IP is read back from the
            docker network-attach response.

      All docker interactions must be mockable — keep them behind
      a thin client adapter the tests can patch. No real docker
      calls in tests.

      Update `tools/local-adapter/AGENT.md` to document the new
      autoscaler responsibility and reference ADR-0060.

      Existing autoscaler tests run against the modified code. If
      a current test asserts a specific worker-spawn invocation,
      update it for the new network + env shape.
    scope:
      files:
        - tools/local-adapter/treadmill_local/autoscaler.py
        - tools/local-adapter/treadmill_local/egress.py
        - tools/local-adapter/tests/test_autoscaler.py
        - tools/local-adapter/tests/test_egress.py
        - tools/local-adapter/AGENT.md
      services_affected:
        - local-adapter
      out_of_scope:
        - services/egress-proxy/
        - workers/agent/
        - infra/cdk/
    validation:
      - kind: deterministic
        description: |
          The local-adapter test suite passes including the new
          egress tests.
        script: |
          uv run --package treadmill-local pytest tools/local-adapter/tests -q
      - kind: deterministic
        description: |
          The autoscaler module references the egress network +
          proxy hostname; the egress module exposes the credential
          minting + config-write helpers.
        script: |
          grep -lE "treadmill-egress" tools/local-adapter/treadmill_local/autoscaler.py
          grep -lE "mint_install_credential|write_worker_allowlist" tools/local-adapter/treadmill_local/egress.py
      - kind: deterministic
        description: |
          AGENT.md describes the autoscaler's new responsibility
          and references ADR-0060.
        script: |
          grep -lE "ADR-0060" tools/local-adapter/AGENT.md

  - id: runner-proxy-credential
    title: "ADR-0060 Step 3c — runner injects install credential into materialize"
    workflow: wf-author
    depends_on: [task.egress-proxy-service.pr_merged]
    intent: |
      STUDY:
        - `workers/agent/treadmill_agent/repo_deps.py` — the existing
          `materialize()` function. Note the subprocess.run sites
          for pip and npm and the urllib.urlopen site for binaries.
        - `workers/agent/treadmill_agent/validation_runtime.py` —
          the subprocess seam that runs validation gates.
        - `workers/agent/treadmill_agent/startup_auth.py` for
          existing per-step env-injection pattern (the per-account
          credential plumb).

      BUILD: in `repo_deps.py`, add a helper
      `_install_proxy_url() -> str | None` that reads
      `TREADMILL_INSTALL_PROXY_TOKEN` from the worker env and the
      base `HTTPS_PROXY` and returns a credentialed proxy URL
      (`http://install:<token>@<host>:<port>`) when both are set,
      else None.

      Update each subprocess.run call inside `materialize()` to pass
      `env={**os.environ, "HTTPS_PROXY": <credentialed>, "HTTP_PROXY":
      <credentialed>}` when the helper returns a URL; otherwise
      pass the current env unchanged. The urllib.urlopen call uses
      `urllib.request.ProxyHandler` with the credentialed URL on
      the same condition.

      `validation_runtime.run_deterministic` does NOT change its
      env handling — it inherits the worker's uncredentialed
      HTTPS_PROXY, which is the task-phase contract.

      Add a unit test in `workers/agent/tests/test_repo_deps.py`
      (or wherever the existing repo_deps tests live) covering:
        - Helper returns None when token absent.
        - Helper returns None when HTTPS_PROXY absent.
        - Helper returns credentialed URL when both present.
        - materialize() subprocess env contains credentialed
          HTTPS_PROXY when token is set (use subprocess.run mock
          + assert on the env kwarg).
        - materialize() subprocess env is unchanged when token is
          unset.

      Add a unit test in `workers/agent/tests/test_validation_runtime.py`
      (or sibling) asserting that the subprocess env passed to
      `run_deterministic` does NOT contain credentialed HTTPS_PROXY
      — the task-phase contract.

      Update `workers/agent/AGENT.md` to document the credential
      flow and reference ADR-0060.
    scope:
      files:
        - workers/agent/treadmill_agent/repo_deps.py
        - workers/agent/tests/test_repo_deps.py
        - workers/agent/tests/test_validation_runtime.py
        - workers/agent/AGENT.md
      services_affected:
        - workers/agent
      out_of_scope:
        - workers/agent/treadmill_agent/validation_runtime.py
        - services/egress-proxy/
        - tools/local-adapter/
    validation:
      - kind: deterministic
        description: |
          The agent test suite passes including the new credential
          tests.
        script: |
          uv run --package treadmill-agent pytest workers/agent/tests -q
      - kind: deterministic
        description: |
          The credential helper exists and is referenced inside
          materialize().
        script: |
          grep -lE "_install_proxy_url|TREADMILL_INSTALL_PROXY_TOKEN" workers/agent/treadmill_agent/repo_deps.py
      - kind: deterministic
        description: |
          AGENT.md documents the credential flow and references
          ADR-0060.
        script: |
          grep -lE "ADR-0060" workers/agent/AGENT.md

  - id: egress-proxy-integration-smoke
    title: "ADR-0060 Step 3d — end-to-end allow/deny integration smoke"
    workflow: wf-author
    depends_on:
      - task.egress-proxy-service.pr_merged
      - task.autoscaler-proxy-wiring.pr_merged
      - task.runner-proxy-credential.pr_merged
    intent: |
      STUDY:
        - The three preceding tasks' new modules.
        - `tools/local-adapter/tests/` for existing integration-test
          fixtures (which already spin up docker-free in-process
          components).

      BUILD: a new integration test file
      `tools/local-adapter/tests/test_egress_proxy_integration.py`
      that:
        - Spawns the proxy in a background asyncio task on an
          ephemeral port (not via docker; in-process).
        - Writes a synthetic worker allowlist config to a temp dir
          (always_allowed=[`localhost`],
          install_allowed=[`localhost`],
          install_credential_hash=sha256 of a known token,
          worker_ip='127.0.0.1').
        - Starts a synthetic upstream HTTP server on a second
          ephemeral port (acts as the registry-side endpoint).
        - Asserts:
          - A CONNECT request via the proxy with install credential
            to the upstream host (in install_allowed) succeeds.
          - A CONNECT request via the proxy WITHOUT install
            credential to the upstream host (in install_allowed
            only, NOT always_allowed) returns 403.
          - A CONNECT request to an unrelated hostname returns 403
            in both phases.
        - The same test should run reliably without docker / real
          network — pytest-level asyncio + sockets only.

      No changes to existing modules in this task (integration test
      only). If a test exposes a real defect that needs a one-line
      fix in the modules from prior tasks, raise it as a follow-up
      task — do not silently expand scope here.
    scope:
      files:
        - tools/local-adapter/tests/test_egress_proxy_integration.py
      services_affected:
        - local-adapter
      out_of_scope:
        - services/egress-proxy/
        - workers/agent/
        - tools/local-adapter/treadmill_local/
    validation:
      - kind: deterministic
        description: |
          The integration test passes.
        script: |
          uv run --package treadmill-local pytest tools/local-adapter/tests/test_egress_proxy_integration.py -q
      - kind: deterministic
        description: |
          The integration test exercises all three of: install-with-
          credential allow, install-without-credential deny, unknown-
          hostname deny.
        script: |
          grep -lE "install.*credential|install_credential" tools/local-adapter/tests/test_egress_proxy_integration.py
          grep -lE "403" tools/local-adapter/tests/test_egress_proxy_integration.py
```

## Diagram

The plan implements ADR-0060's architecture; the diagram lives there
(the same flowchart-of-components view) and we do not duplicate it
here. See `docs/adrs/0060-sidecar-https-proxy-for-worker-egress-scoping.md`
for the proxy + worker + autoscaler topology.

## Risks / unknowns

- **Proxy library choice.** Stdlib asyncio is sufficient for a
  CONNECT proxy; aiohttp adds polish for headers + structured logs.
  Worker chooses; both are acceptable. **Abort trigger:** none —
  reviewable in PR.
- **Docker network creation in dev-local.** The autoscaler may not
  currently have permission to create networks in some dev setups.
  **Mitigation:** the autoscaler should detect the `treadmill-egress`
  network's existence and skip creation if present; document the
  required permission in the AGENT.md update.
- **Credential leakage via env inheritance.** A subprocess in a
  validation gate could in principle echo `os.environ` and surface
  `TREADMILL_INSTALL_PROXY_TOKEN`. The credential is per-worker,
  per-task, short-lived; even a leaked one only opens install-phase
  egress, which is itself a curated allowlist. Acceptable under the
  determinism framing; revisit in B-track.
- **Production EC2 surface.** This plan targets dev-local only; the
  cdk infra changes for prod EC2 land in a follow-up plan once the
  dev-local pattern proves out. **Abort trigger:** if the dev-local
  approach reveals a fundamental issue (e.g. docker `internal=True`
  networks don't behave as documented), revisit the architecture
  before extending to prod.

## Decisions captured during execution

(empty at draft time; appended as work progresses)

## Post-mortem

(filled when plan transitions to completed)
