- **[#TBD] Per-host supervisor drift guard (task 030872f5, ADR-0095)**: adds
  `tools/cc-channels/host_guard.py` — stdlib-only pure decision module:
  `refuse_reason(label, local_host, bound_host) -> str | None` and
  `local_host_name()` (reads `TREADMILL_HOST`). Wires a host-binding preflight
  into `treadmill-channel-launch` (after PID singleton check, before tmux
  creation): when `TREADMILL_HOST` is set, curls
  `TREADMILL_API_URL/api/v1/hosts/bindings/<label>`, calls
  `host_guard.py check <label> <bound_host>`, and exits 1 on refusal
  (fail-closed: unbound or bound to a different host). ADR-0097 host-proof auth
  is out of scope; `TREADMILL_HOST` is trusted as-is. Tests in
  `tools/cc-channels/tests/test_host_guard.py`.
