# services/api/treadmill_api/operator_access

## Purpose

Identity ACL and bind-address utilities for the Tailscale terminal bridge (ADR-0097).
Implements two decisions:

- **Decision 3 (bind):** Services bind to the Tailscale interface only — never a
  wildcard, loopback, or LAN address. `tailnet_bind_addr()` raises `ValueError` at
  startup if an illegal address is passed. Tailscale Funnel is never enabled.
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
- **`bind.py`** — `tailnet_bind_addr(candidate) -> str`: raises `ValueError` for
  empty string, `"localhost"`, loopback, and wildcard/unspecified addresses; returns
  Tailscale CGNAT addresses unchanged (100.64.0.0/10). `funnel_enabled() -> bool`:
  constant `False` — Tailscale Funnel is never enabled (ADR-0097 §Decision 4).

## Tests

`services/api/tests/test_operator_access.py` — behavioral tests (is_operator
allow/deny, tailnet_bind_addr CGNAT + banned raises, funnel_enabled=False) plus
the static falsifier: `test_operator_access_source_is_clean` scans
`operator_access/*.py` for wildcard bind literals and Funnel-enable patterns, with
`test_wildcard_scan_detects_violation` as the non-vacuous meta-check.

## Recent changes

> **New entries are PER-PR FRAGMENT FILES, not prepends** (task 5bfd5489):
> add `agent-changes/YYYY-MM-DD-<task-id>-<slug>.md` beside this AGENT.md —
> one entry per file, newest by filename; format in `docs/agent-md-schema.md`.
> Prepending here is the conflict factory.

## Navigation

- **Parent:** `services/api/treadmill_api/` — FastAPI application package.
- **Decisions:** ADR-0097 (operator surfaces over tailnet, tailnet-only bind,
  Funnel-never, single-operator-identity ACL).
