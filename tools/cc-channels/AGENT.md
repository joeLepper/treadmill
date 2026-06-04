# tools/cc-channels

## Purpose

Per-session Claude Code channel launcher. Provides `launch-session.sh` (the shared
entry point for labelled sessions), `cc-access.py` (Telegram access management), and
the ADR-0073 persistent-session substrate (systemd unit, tmux launcher, `cc-attach`).

## Key surfaces

- `launch-session.sh` — launch or resume a labelled CC session with the treadmill-events
  and optional Telegram channels. Accepts `<label> [workdir] [-- extra-claude-args]` or
  `--resume-by-label <label>` (supervised path). Writes `~/.cc-channels/<label>/launcher.pid`
  before invoking Claude; removes it on exit.
- `cc-access.py` — Telegram per-session access manager. Use this instead of the stock
  `/telegram:access` skill (which targets the wrong state dir under per-bot isolation).
- `systemd/treadmill-channel@.service` — parameterized systemd user unit; instance = label.
  Install to `~/.config/systemd/user/`.
- `systemd/treadmill-channel-launch` — `ExecStart` target; idempotently creates/reuses
  the tmux session and supervises it as a foreground process.
- `bin/cc-attach` — `exec tmux attach-session -t <label>`; the operator interface for
  attaching to a supervised session.
- `tests/test_launcher_pidfile.sh` — unit tests for the PID-file single-instance guard.

## State layout

```
~/.cc-channels/<label>/
  session-id       # per-label CC session ID (mint on first launch, reuse thereafter)
  launcher.pid     # PID of the running launch-session.sh (written/removed by it)
  telegram/        # Telegram plugin state dir (TELEGRAM_STATE_DIR)
  telegram.env     # optional: TELEGRAM_BOT_TOKEN=...
  treadmill/       # treadmill-events channel cursor + dedup window
```

## Recent changes

- [ADR-0073 Step 1](#) — added persistent-session substrate: `systemd/treadmill-channel@.service`,
  `systemd/treadmill-channel-launch`, `bin/cc-attach`, PID-file single-instance guard in
  `launch-session.sh`, `tests/test_launcher_pidfile.sh`, and this AGENT.md.
  Design: systemd-user supervises `treadmill-channel-launch`, which owns a tmux session;
  `launch-session.sh` runs inside the tmux window and manages the PID file.

## Decisions

- ADR-0067 — CC Channels, one bot per session (phone access)
- ADR-0068 — treadmill-events channel and shared channel conventions
- ADR-0071 — relay log levels (TREADMILL_RELAY_LEVEL)
- ADR-0073 — persistent orchestrator sessions and interactive attach

## Pitfalls

- **Do not use `/telegram:access`** inside these sessions — it targets
  `~/.claude/channels/telegram/` (the default singleton path), not the per-label
  `~/.cc-channels/<label>/telegram/`. Use `cc-access.py --label <label>` instead.
- **`exec claude` was intentionally removed** from `launch-session.sh`'s final line.
  Using `exec` would replace the shell process with Claude, preventing the EXIT trap
  from running and leaving a stale `launcher.pid` file. The shell must stay alive to
  supervise the cleanup.
- **The `TREADMILL_REPO_DIR` env var** must be set (via systemd drop-in) if the repo
  is not cloned at `~/treadmill`.
