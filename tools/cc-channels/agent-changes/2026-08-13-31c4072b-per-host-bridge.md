- **[#PR] Per-host WebSocket terminal bridge (task 31c4072b, ADR-0097)**: new
  `tools/cc-channels/bridge.py`. Binds ONLY to the Tailscale CGNAT address via
  `tailnet_bind_addr()` (imported from `treadmill_api.operator_access.bind` — no
  reimplementation). Gates EVERY WebSocket connection with `is_operator()` (imported
  from `treadmill_api.operator_access.acl`) before any `recv()`. Attaches ONLY to
  named existing local tmux sessions (`tmux ls`); never spawns a shell; cross-host
  sessions are unreachable by construction (local list only). Fixed port 7681.
  Detach closes the socket and leaves the session running (ADR-0097 §5).
  Launcher integration: `treadmill-channel-launch` starts one bridge singleton per
  host (pidfile at `~/.cc-channels/bridge.pid`; gate: TREADMILL_HOST +
  TREADMILL_OPERATOR_IDENTITY both set). Tests: 11 tests — red-then-green proof for
  identity gate and session gate (naive handler fails, real handler passes); allow
  path confirmed; cross-host refused; bind gate parametrized (10 banned addresses);
  bind naive red proof; CGNAT positive; PORT constant.
