- **[#PR] Per-host WebSocket terminal bridge (task 31c4072b, ADR-0097)**: new
  `tools/cc-channels/bridge.py`. IMPORTS (does not reimplement) `tailnet_bind_addr()`
  from `treadmill_api.operator_access.bind` and `is_operator()` from
  `treadmill_api.operator_access.acl`. Security invariants (all fail-closed):
  (1) Bind ALLOWLIST — `tailnet_bind_addr()` raises for any address outside
  100.64.0.0/10; bridge startup aborts before any socket opens.
  (2) Identity gate — every WebSocket connection is gated by `is_operator()`
  before `recv()` is called; non-operator is closed with code 1008 immediately.
  (3) Session gate — after identity check, the client sends a session name that
  is validated against `tmux ls` (local-only); unknown or cross-host sessions
  are closed with code 1011; `open_pty_attach` is never called for missing sessions.
  (4) No shell spawn — the bridge runs `tmux attach-session -t <name>` only; the
  session name is never interpolated into a shell command.
  Launcher: `treadmill-channel-launch` starts one bridge per host via pidfile
  singleton (gate: TREADMILL_HOST + TREADMILL_OPERATOR_IDENTITY; TREADMILL_BRIDGE_PORT
  overrides port 7681). Tests: 11 tests with red-then-green proof for identity and
  session gates; bind gate covers 10 banned address classes.
