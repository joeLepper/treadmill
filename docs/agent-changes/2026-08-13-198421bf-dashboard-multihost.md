- **[#PR] Multi-host dashboard agent list (task 198421bf, ADR-0097 §Decision 2)**:
  new `services/api/treadmill_api/routers/dashboard/agents.py` —
  `GET /api/v1/dashboard/agents`. Lists all session agents by `(label, host)` across
  enrolled hosts. Reads `(label→host)` + `host.tailnet_addr` via ADR-0095's
  `HostRegistryStore.list_agents` (new method — LEFT OUTER JOIN of
  `session_host_bindings` onto `hosts`). Security invariants: READ-ONLY (dashboard
  never writes the host store); NULL `tailnet_addr` → `reachable=False`,
  `bridge_ws_url=null` (UNREACHABLE, no connect attempt, no guessed address);
  bridge URL derived from registry `tailnet_addr` only (ADR-0097 §Decision 3 —
  NEVER hardcode an address); fixed bridge port 7681. Auto-discovered by the
  dashboard package's `__init__.py` — no `__init__.py` edit required. Tests: 14 tests
  — red-then-green for NULL tailnet_addr guard and static-vs-dynamic host list;
  read-only spy; no-hardcoded-address static scan; auto-discovery pin; store method
  check; 1284/1287 API suite passes (3 pre-existing OTLP failures unrelated).
  Also adds `routers/dashboard/AGENT.md` documenting the dashboard package and the
  multi-host addressing model.
