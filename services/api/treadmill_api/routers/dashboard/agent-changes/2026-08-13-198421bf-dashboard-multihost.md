- **[#PR] Multi-host dashboard agent list (task 198421bf, ADR-0097 §Decision 2)**:
  new `routers/dashboard/agents.py` — `GET /api/v1/dashboard/agents`. Lists all
  session agents by `(label, host)` across enrolled hosts. Reads `(label→host)` +
  `host.tailnet_addr` from ADR-0095's `HostRegistryStore.list_agents` (new method,
  single LEFT OUTER JOIN on `session_host_bindings` + `hosts`). Auto-discovered by
  dashboard package — no `__init__.py` edit needed. Security invariants: READ-ONLY
  (no host store writes from dashboard); NULL `tailnet_addr` → `reachable=False`,
  `bridge_ws_url=null` (UNREACHABLE, no connect attempt); bridge URL derived from
  registry `tailnet_addr` only — no address hardcoded (ADR-0097 §Decision 3);
  fixed bridge port 7681 surfaced as `bridge_port` in response. Tests: 14 tests —
  red-then-green for NULL tailnet_addr guard and registry-vs-static-config; read-only
  spy; no-hardcoded-address static scan; auto-discovery pin; host store method check.
  Also creates `routers/dashboard/AGENT.md` describing the dashboard package.
