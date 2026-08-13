- **[#TBD] Per-host supervisor drift guard (task 030872f5, ADR-0095)**:
  `tools/cc-channels/host_guard.py` — stdlib-only pure decision module with
  `refuse_reason(label, local_host, bound_host) -> str | None` (fail-closed:
  unbound → refuse; bound elsewhere → refuse; bound here → allow) and
  `local_host_name()` (reads `TREADMILL_HOST` env var; ADR-0097 trust model).
  `treadmill-channel-launch` gains a host-binding preflight between the PID
  singleton check and tmux session creation: when `TREADMILL_HOST` is set, curl
  fetches `TREADMILL_API_URL/api/v1/hosts/bindings/<label>` and
  `host_guard.py check` refuses start if the binding is absent or wrong. Guard
  is inert when `TREADMILL_HOST` is unset (single-host back-compat). Tests:
  `tools/cc-channels/tests/test_host_guard.py` (5 tests: 3 unit, 2 CLI).
