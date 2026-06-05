# cc-channels — labelled Claude Code orchestrator sessions

This directory holds the operator-facing launcher and supervision substrate for
Treadmill orchestrator sessions: one persistent Claude Code conversation per
label, paired with its own Telegram bot and `treadmill-events` channel.

- `launch-session.sh` — labelled launcher (ADR-0067/0068). Sets the session
  label env, attaches the `treadmill-events` and (optional) Telegram channels,
  and `exec`s Claude with `--resume <session-id>` so the same conversation
  re-loads across launches. Enforces a single-instance contract via
  `~/.cc-channels/<label>/launcher.pid` (ADR-0073).
- `cc-access.py` — per-session Telegram access manager (multi-bot caveat
  documented in `tools/cc-channel-treadmill/README.md`).
- `systemd/treadmill-channel@.service` + `systemd/treadmill-channel-launch` —
  systemd-user template unit and its launch wrapper (ADR-0073). Owns liveness
  for `treadmill-channel@<label>.service` instances.
- `bin/cc-attach` — operator-facing attach wrapper around `tmux a -t <label>`.

## Persistent sessions (ADR-0073)

One-time setup:

```
loginctl enable-linger $USER
mkdir -p ~/.config/systemd/user
cp tools/cc-channels/systemd/treadmill-channel@.service \
  ~/.config/systemd/user/
sudo install -m 0755 tools/cc-channels/bin/cc-attach /usr/local/bin/
```

The unit's `ExecStart` resolves the repo via `$HOME/treadmill` by default.
If you cloned Treadmill elsewhere, export `TREADMILL_REPO_DIR` for the
systemd-user manager (e.g. `systemctl --user set-environment TREADMILL_REPO_DIR=/path/to/treadmill`)
before enabling the unit.

Per label:

```
systemctl --user enable --now treadmill-channel@bert.service
cc-attach bert
```

- **Detach without killing:** `Ctrl-b d`
- **Stop the supervised session:**
  `systemctl --user stop treadmill-channel@bert.service`
- **Recover from a stale pidfile** (e.g. after a kernel panic): the launcher
  detects a dead PID via `kill -0` and cleans up automatically; manual `rm
  ~/.cc-channels/<label>/launcher.pid` is only needed in the rare PID-reuse case.

The wrapper and `launch-session.sh` both enforce the single-instance contract,
so an operator who runs `launch-session.sh` directly cannot race the systemd-
supervised instance.

**Crash recovery.** Claude-process crashes, OOM kills, and externally-killed
tmux sessions are recovered by systemd's `Restart=on-failure` after
`RestartSec=5s`. Operator-initiated `systemctl --user stop` does NOT respawn —
the wrapper traps SIGTERM/SIGINT and exits 0 so the unit stays stopped.
