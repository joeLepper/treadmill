# services/api/treadmill_api/operator_access

## Purpose

Identity ACL and bind-address utilities for the Tailscale terminal bridge (ADR-0097).
Implements two decisions:

- **Decision 3 (bind):** Services bind to the Tailscale interface only.
  `tailnet_bind_addr()` is an ALLOWLIST — it accepts ONLY Tailscale CGNAT
  addresses (100.64.0.0/10) and raises `ValueError` for everything else,
  including LAN, public IPs, loopback, wildcard, hostnames, and case-variants
  (fail-closed / allow-only-tailnet). Tailscale Funnel is never enabled.
- **Decision 4 (ACL):** Access control is Tailscale's ACL plus Tailscale identity.
  No authentication system is authored here; no tokens or passwords are issued.
  This module narrows the tailnet boundary to a single configured operator identity
  to shrink blast radius.

ADR-0097 trust model: Tailscale supplies the caller identity; `is_operator()` only
compares it. `TREADMILL_HOST` proof/auth is ADR-0097 §Decision 3 (out of scope here).

## Modules

- **`acl.py`** — `is_operator(identity, *, operator_identity) -> bool`: the ONLY
  identity gate. `True` iff the caller's Tailscale identity exactly equals the
  configured operator identity. Empty, `None`, or any unknown identity is denied.
- **`bind.py`** — `tailnet_bind_addr(candidate) -> str`: **ALLOWLIST** — accepts
  ONLY addresses in the Tailscale CGNAT range `100.64.0.0/10` (via
  `ipaddress.IPv4Network` membership); raises `ValueError` for everything else:
  LAN (192.168.x.x, 10.x.x.x), public IPs, loopback, wildcard, non-IP hostnames,
  and case-variants (fail-closed / allow-only-tailnet). `funnel_enabled() -> bool`:
  constant `False` — Tailscale Funnel is never enabled (ADR-0097 §Decision 4).

## Tests

`services/api/tests/test_operator_access.py` — 19 tests: behavioral (is_operator
allow/deny, tailnet_bind_addr CGNAT pass + 10 banned cases including LAN/public/
hostname/case-variants all raise, funnel_enabled=False) plus the static falsifier:
`test_operator_access_source_is_clean` scans `operator_access/*.py` for wildcard
bind literals and Funnel-enable patterns, with `test_wildcard_scan_detects_violation`
and `test_funnel_scan_detects_violation` as the non-vacuous meta-checks.

## Recent changes

> **New entries are PER-PR FRAGMENT FILES, not prepends** (task 5bfd5489):
> add `agent-changes/YYYY-MM-DD-<task-id>-<slug>.md` beside this AGENT.md —
> one entry per file, newest by filename; format in `docs/agent-md-schema.md`.
> Prepending here is the conflict factory.

## Navigation

- **Parent:** `services/api/treadmill_api/` — FastAPI application package.
- **Decisions:** ADR-0097 (operator surfaces over tailnet, tailnet-only bind,
  Funnel-never, single-operator-identity ACL).
