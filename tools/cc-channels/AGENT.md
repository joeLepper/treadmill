# tools/cc-channels

## Purpose

This directory holds the operator-facing launcher and supervision substrate for
Treadmill orchestrator sessions: one persistent Claude Code conversation per
label, with its own Telegram bot and `treadmill-events` channel. The session
label is the routing-and-identity primitive — it names the bot, keys the state
dirs under `~/.cc-channels/<label>/`, and equals the `--created-by` the session
passes on `treadmill plan submit` so the events channel receives its own work
(ADR-0068). Under ADR-0073 this directory also owns the systemd-user + tmux +
`cc-attach` substrate that keeps each labelled session alive across SSH drops,
terminal closes, and crashes.

## Key surfaces

- `launch-session.sh` — entry point. Mints / resumes a stable Claude Code
  session id, attaches the `treadmill-events` channel and (when configured)
  the Telegram channel, and `exec`s `claude`. Writes its own PID to
  `~/.cc-channels/<label>/launcher.pid` immediately before `exec` and refuses
  to start when a live PID is already present (ADR-0073 single-instance
  contract).
- `cc-access.py` — per-session Telegram access manager. Targets the
  per-session `access.json` that the channel server actually reads; use this
  instead of the stock `/telegram:access` skill, which hardcodes the
  default state dir.
- `systemd/treadmill-channel@.service` — systemd-user template unit; one
  instance per label.
- `systemd/treadmill-channel-launch` — wrapper invoked by the unit. Creates
  the tmux session if missing, sends the launcher into it, and stays in the
  foreground while the session exists (so systemd treats the service as live).
- `bin/cc-attach` — thin `tmux a -t <label>` wrapper. The operator-facing
  attach command.
- `tests/test_launcher_singleton.py` — pytest for the launcher's
  PID-file refusal path (no tmux/systemd/claude invoked).

## Recent changes

- ADR-0073 Step 1 — systemd-user + tmux + `cc-attach` substrate for persistent,
  attachable orchestrator sessions; launcher gained the `launcher.pid`
  single-instance guard. Adds `systemd/`, `bin/cc-attach`, and the singleton test.
- [#161](https://github.com/anthropics/treadmill/pull/161) — CLI resolves
  `--created-by` from `TREADMILL_SESSION_LABEL` (set by `launch-session.sh`)
  and warns on mismatch.
- ADR-0067/0068 + PR #147 — initial labelled-session launcher, per-bot
  Telegram state isolation, and the `treadmill-events` channel wiring.

## Pitfalls

- The Telegram plugin keeps a `bot.pid` singleton guard in its state dir.
  Two bots sharing a dir kill each other; the launcher gives each label its
  own `TELEGRAM_STATE_DIR` to avoid that. Do not collapse state dirs back to
  the default `~/.claude/channels/telegram/`.
- The stock `/telegram:access` skill silently edits the wrong (empty) file
  under the per-bot layout. Use `cc-access.py --label <label> ...` instead.
- `launch-session.sh` uses `exec claude` — bash `trap EXIT` handlers do not
  fire across `exec`. Do not add cleanup logic that depends on traps after
  the exec point; stale `launcher.pid` files are cleaned up on next start
  via the `kill -0` check, by design.
- The systemd unit assumes the repo at `$HOME/treadmill`. Override via
  `TREADMILL_REPO_DIR` (`systemctl --user set-environment ...`) before
  enabling on hosts where the clone path differs.
- Claude Code channels are a **research preview**. Re-verify the
  `--channels` / `--dangerously-load-development-channels` / `--resume`
  contract after CC upgrades; the launcher is currently pinned against
  CC 2.1.161.
- Sessions launch with `--dangerously-skip-permissions`. The Telegram sender
  allowlist (via `cc-access.py policy allowlist`) is **mandatory**, not
  optional — an ungated inbound channel message is direct code execution.

## Navigation

- **Adjacent:** `tools/cc-channel-treadmill/` (the `treadmill-events` MCP
  server this launcher loads); `cli/` (resolves `--created-by` from the
  label env this launcher sets).
- **Decisions:** ADR-0067 (phone-access channels), ADR-0068 (treadmill-events
  as in-session event bus), ADR-0071 (operator notification two-layer strategy),
  ADR-0073 (persistent orchestrator sessions + interactive attach).
- **Follow:** Start with ADR-0073 for the supervision substrate; read
  `tools/cc-channel-treadmill/README.md` for the channel-server side.
