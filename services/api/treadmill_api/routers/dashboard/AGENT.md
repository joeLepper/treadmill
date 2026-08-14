# treadmill_api/routers/dashboard

## Purpose

Operator dashboard API endpoints (ADR-0056 + ADR-0097). Backs the static React SPA at
`services/dashboard/`. Each endpoint module defines `router = APIRouter()` at the
module level and is auto-discovered by `__init__.py` — no edit to `__init__.py` is
needed when adding a new module.

## Key surfaces

- `overview.py` — `GET /api/v1/dashboard/overview`. Aggregates tasks (by bucket),
  escalations, events, accounts, and fleet. Mirrors the `useOverview` query shape in
  `services/dashboard/src/api/queries.ts`. Filters: `repo`, `bucket`, `account`, `q`,
  `reason`, `include_closed`.
- `agents.py` — `GET /api/v1/dashboard/agents`. Multi-host agent list (ADR-0097
  §Decision 2). Lists all session agents by `(label, host)` across enrolled hosts,
  resolving `host → tailnet_addr` via ADR-0095's `HostRegistryStore`. Returns a
  bridge WebSocket URL for reachable hosts (`ws://<tailnet_addr>:7681/`) and
  `reachable=False` + `bridge_ws_url=null` when `tailnet_addr` is NULL. READS the
  host store — never writes. Never hardcodes an address (ADR-0097 §Decision 3).
- `task_detail.py` — `GET /api/v1/dashboard/tasks/{task_id}`. Per-task detail.
- `ws.py` — `/api/v1/dashboard/ws/events`. Server-sent event stream.
- `ack_escalation.py` — `POST /api/v1/dashboard/escalations/{task_id}/ack`. Dismiss
  an open escalation.
- `cancel.py` — `POST /api/v1/dashboard/tasks/{task_id}/cancel`. Cancel a task.
- `repo_docs.py` — `GET /api/v1/dashboard/repo_docs/{task_id}`. Per-task docs.

## Multi-host addressing (`agents.py`, ADR-0097)

An agent is addressed by `(label, host)`. The dashboard lists agents across all enrolled
hosts by reading ADR-0095's `session_host_bindings` and `hosts` tables (via
`HostRegistryStore.list_agents`). It does NOT maintain its own host list.

Opening a terminal for `(label, host)`:
1. The frontend reads `bridge_ws_url` from the `GET /api/v1/dashboard/agents` response.
2. The frontend connects to that URL (the host's bridge, ADR-0097 Task 2, `bridge.py`).
3. The bridge gates the connection with `is_operator()` before any PTY attach.
4. The dashboard introduces NO new unauthenticated path to a host terminal.

A `NULL` `tailnet_addr` means the host has not registered a Tailscale address.
`bridge_ws_url` is `null` and `reachable` is `false`. The client must NOT attempt
a connect. The address is never guessed or defaulted.

## Auto-discovery contract

Every sibling `.py` that exports `router = APIRouter()` is mounted under
`/api/v1/dashboard` at import time by `__init__.py._discover_and_mount`. Adding a
new endpoint file requires no edit to `__init__.py` or `app.py`.

## Recent changes

> **New entries are PER-PR FRAGMENT FILES, not prepends** (task 198421bf):
> add `agent-changes/YYYY-MM-DD-<task-id-prefix>-<slug>.md` beside this AGENT.md —
> one entry per file, newest by filename; format in `docs/agent-md-schema.md`.
> Prepending here is the conflict factory.

## Navigation

- **Store:** `treadmill_api/host_registry_store.py` — `HostRegistryStore.list_agents`
  reads `(label, host, tailnet_addr)` via a single JOIN (added for ADR-0097).
- **Models:** `treadmill_api/models/host.py` — `Host`, `SessionHostBinding`.
- **Routers parent:** `treadmill_api/routers/AGENT.md`.
- **Decisions:** ADR-0056 (dashboard scope); ADR-0095 (named agents bind to hosts);
  ADR-0097 (operator surfaces reach any agent over the tailnet — §Decision 2 is the
  multi-host dashboard; §Decision 3 is the tailnet-only bind/address rule).
- **Bridge:** `tools/cc-channels/bridge.py` — the per-host WebSocket terminal
  bridge (fixed port 7681) that the `bridge_ws_url` points at.
