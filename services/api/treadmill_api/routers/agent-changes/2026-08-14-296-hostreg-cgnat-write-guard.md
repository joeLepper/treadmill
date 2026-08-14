# 2026-08-14 — register_host rejects non-CGNAT tailnet_addr at write time (ADR-0097 / #296)

PR: closes [#296](https://github.com/joeLepper/treadmill/issues/296)

`HostRegistryStore.register_host` now guards `tailnet_addr` at write time. A
non-NULL `tailnet_addr` MUST be a Tailscale CGNAT address (100.64.0.0/10); a
poisoned non-tailnet address (LAN, public IP, hostname) raises `ValueError`
before the upsert, so it can never reach the `hosts` table. A NULL
`tailnet_addr` is still allowed — a host that has not yet self-reported its
address.

The check reuses the single-source-of-truth predicate
`treadmill_api.operator_access.is_tailnet_address`; no CGNAT range literal is
duplicated in the store. This completes the ADR-0097 write/read/connect triad:
the same predicate now guards register-write (this change),
dashboard-read (`list_agents` / `agents.py`), and bind-connect (`bind.py`).
