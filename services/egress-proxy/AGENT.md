# treadmill-egress-proxy

HTTP CONNECT proxy that enforces per-worker egress allowlists for Treadmill
worker containers. Implements the proxy side of the network isolation design
in ADR-0060.

## Responsibilities

- Accept HTTP CONNECT requests from worker containers.
- Look up the source worker IP against a per-worker allowlist config loaded
  from a configurable directory.
- Apply two-phase access control:
  - **always_allowed**: hostnames reachable without a credential (task phase
    and install phase) — Anthropic API, GitHub, Treadmill API.
  - **install_allowed**: hostnames additionally reachable when the request
    carries a matching install-phase credential (PyPI, npm registries, binary
    download hosts).
- Log every allow/deny decision as a JSON line to stdout.
- Return `HTTP/1.1 403 Forbidden` for denied requests, naming the rule that
  failed.
- Return `HTTP/1.1 200 Connection established` and tunnel for allowed
  requests.

## Config file shape

One JSON file per worker under `EGRESS_PROXY_CONFIG_DIR` (default
`/etc/treadmill/egress-proxy`). The autoscaler writes these files; the proxy
picks them up on `mtime` change.

```json
{
  "worker_ip": "172.18.0.5",
  "always_allowed": ["api.anthropic.com", "api.github.com"],
  "install_allowed": ["pypi.org", "files.pythonhosted.org"],
  "install_credential_hash": "<sha256-hex-of-install-token>"
}
```

Fields:
- `worker_ip` — the docker-bridge IP the config applies to; used as the
  lookup key when a connection arrives.
- `always_allowed` — hostnames reachable in both phases (no credential
  required).
- `install_allowed` — hostnames reachable only when `Proxy-Authorization`
  Basic auth password hashes (SHA-256) to `install_credential_hash`.
- `install_credential_hash` — SHA-256 hex digest of the per-worker install
  credential minted by the autoscaler.

## Stdout log line format

Each connection decision writes one JSON object:

```json
{
  "ts": "2026-05-28T12:00:00.000000+00:00",
  "worker_ip": "172.18.0.5",
  "hostname": "pypi.org",
  "phase": "install",
  "decision": "allow",
  "reason": "install_allowed"
}
```

`phase` values: `always`, `install`, `none`, `unknown`
`decision` values: `allow`, `deny`
`reason` values: `always_allowed`, `install_allowed`, `credential_required`,
`credential_mismatch`, `hostname_not_allowed`, `no_config_for_worker`

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `EGRESS_PROXY_CONFIG_DIR` | `/etc/egress-proxy-config` | Directory containing per-worker `*.json` allowlist files. |
| `EGRESS_PROXY_PORT` | `3128` | TCP port the proxy listens on. |

## Container image

Built locally from `services/egress-proxy/Dockerfile` (build context
is `services/egress-proxy/`) as `treadmill-egress-proxy:dev` by
`tools/local-adapter/treadmill_local/runtime.py::_ensure_images_built`
during every `treadmill-local up`. The autoscaler in
`tools/local-adapter/treadmill_local/egress_proxy.py` (ADR-0060
Step 3b) spawns one container per host on the
`treadmill-egress` internal docker network, mounting the operator-
side config directory read-only at `/etc/egress-proxy-config`. Per
ADR-0064 Step 2, `ensure_egress_proxy_container` then multi-attaches
the proxy to the `treadmill-local` network after spawn so its
outbound CONNECTs route through that network's gateway (the egress
bridge is `internal=True` and has no upstream gateway of its own).

The Dockerfile is intentionally minimal — pure stdlib asyncio plus a
single Pydantic dep means no system packages, no compile step, no
network installs beyond `pip install --no-cache-dir .` of the local
wheel. `EXPOSE 3128` matches the autoscaler's spawn assumption.

## Recent changes

- ADR-0065 Step 2 — `autoscaler_smoke.yml` removed 2026-06-05. The
  workflow had never passed in its 4-run history (the CDK app returns
  no stacks in fully-local mode and the runtime calls `cdk synth`
  expecting stacks, so `treadmill-local up` always crashed in CI). The
  on-host script (`scripts/smoke_boot.sh`) still exists; restoring the
  gate requires reconciling app.py vs runtime.py on fully-local
  provisioning. Until then, this service has no automated boot
  coverage — assertions about CONNECT allow / 403 deny live only in
  the manual smoke path.
- ADR-0064 Step 2: `ensure_egress_proxy_container` multi-attaches
  the proxy container to `treadmill-local` immediately after the
  initial spawn on `treadmill-egress`. Without this the proxy could
  not reach upstream targets at all — the egress bridge is
  `internal=True` and has no gateway, so without a second
  attachment to a non-internal network every CONNECT terminated at
  the bridge. Implementation in
  `tools/local-adapter/treadmill_local/egress_proxy.py` imports
  `runtime.NETWORK_NAME` lazily inside the function to avoid a
  module-load circular import.
- Dockerfile + image-build wiring (closes the 2026-06-02 delivery
  gap from ADR-0060 Step 3b — the autoscaler-spawn code was merged
  in PR #92 but the image it referenced wasn't built anywhere in
  dev-local, so the autoscaler 404'd on `docker pull` and no
  workers spawned).
- Default `EGRESS_PROXY_CONFIG_DIR` aligned with the autoscaler's
  spawn mount (`/etc/egress-proxy-config`), so the proxy reads
  per-worker configs out-of-the-box without env-var wiring.
- PR #77 — Initial implementation (ADR-0060 Step 3a).
