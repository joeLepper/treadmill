# 2026-08-13 — Host registry and per-label host binding model (ADR-0095)

PR: TBD (opened by worker-joelepper-treadmill-1, task 5e6c2383)

## What changed

- Added `services/api/treadmill_api/models/host.py` with two ORM models:
  - `Host` — registry of named hosts. Columns: `id` (uuid pk), `name` (unique string), `tailnet_addr` (nullable — Tailscale IPv4 for ADR-0097), `created_at`.
  - `SessionHostBinding` — maps a session label to a host. Columns: `label` (pk), `host`, `bound_at`, `bound_by` (nullable audit field).
- Added `services/api/treadmill_api/host_registry_store.py` — `HostRegistryStore` with six methods: `register_host`, `list_hosts`, `bind_label`, `host_for_label`, `labels_on_host`, `unbind_label`.
- Added `services/api/treadmill_api/routers/hosts.py` — six REST endpoints under `/api/v1/hosts` and `/api/v1/hosts/bindings`.
- Added `services/api/alembic/versions/20260813_0100_host_registry.py` — creates `hosts` and `session_host_bindings` tables. `down_revision = "20260612_0200"`.
- Registered `Host` and `SessionHostBinding` in `models/__init__.py`.
- Registered `hosts_router` in `app.py`.
- Added `services/api/treadmill_api/routers/AGENT.md` documenting the routers directory and host endpoints.
- Added `services/api/tests/test_host_registry_model.py` — shape tests (always run) + integration round-trips (gated on `TREADMILL_INTEGRATION=1`).

## Why

ADR-0095: named agents must bind to specific hosts so the supervisor on each
host knows which sessions to start, and so ADR-0097's Carla operator plane
can resolve label → host → tailnet address → WebSocket endpoint. The label
addresses the session; the host says where it runs. Binding is explicit-only —
no automatic rebind on host-unreachable.

The `tailnet_addr` column was added per operator note (2026-08-13, driver donna):
host registers its Tailscale IPv4 on each call to `POST /api/v1/hosts`; Carla
reads this to open the per-host bridge WebSocket.
