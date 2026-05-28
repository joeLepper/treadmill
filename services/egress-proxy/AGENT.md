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
| `EGRESS_PROXY_CONFIG_DIR` | `/etc/treadmill/egress-proxy` | Directory containing per-worker `*.json` allowlist files. |
| `EGRESS_PROXY_PORT` | `3128` | TCP port the proxy listens on. |

## Recent changes

- PR #TBD — Initial implementation (ADR-0060 Step 3a).
