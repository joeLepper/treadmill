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
- `cc-relay.py` — inter-session Telegram relay. Sends a message or file to
  another named session's Telegram channel. Requires `TELEGRAM_CHAT_ID` in the
  target's `telegram.env` (one-time setup).
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

- [#169](https://github.com/joeLepper/treadmill/pull/169) ADR-0073 Step 1 follow-up
  — `treadmill-channel-launch` now fails loud with platform-specific install
  hints when `tmux` is absent (apt / pacman / brew); guards the late-stage
  `tmux has-session` failure mode. Precheck test added in
  `tests/test_launcher_singleton.py`. (The cwd-handling piece this PR also
  originally carried was superseded by the workdir state file in #188.)
- ADR-0073 Step 1 cwd persistence — `launch-session.sh` now writes its
  resolved `$WORKDIR` to `$STATE_ROOT/workdir` before `exec claude`, and the
  systemd wrapper reads that file and passes it to `tmux new -d -s -c
  <workdir>`. Missing file → `$HOME/treadmill` fallback. Without this, the
  supervised restart path (now structurally working after #184/#186/#187)
  silently lost the session on every crash: systemd-user runs the wrapper
  with cwd=$HOME, `tmux new -d -s` inherited that, `claude --resume <sid>`
  ended up at `/home/joe`, couldn't find the per-label transcript under
  `-home-joe-treadmill/` (or whatever the label's downstream repo slug is),
  and opened
  a fresh trust-prompt session. The crash test now resumes cleanly without
  operator hands.
- ADR-0073 Step 1 set-e silent-exit fix — `STATE=$(ps -p $PID -o state= |
  head -c 1 || true)` in both singleton-check sites. Without `|| true`,
  `set -euo pipefail` killed the wrapper silently any time the recorded
  PID was dead (the case the singleton check was supposed to recover
  from). systemd looped without ever producing stderr.
- ADR-0073 Step 1 zombie-PID fix — both the systemd wrapper
  (`systemd/treadmill-channel-launch`) and the launcher
  (`launch-session.sh`) singleton checks now treat a `Z` (zombie) process state
  as "dead" for the purposes of the launcher.pid guard. `kill -0` reports
  success on zombies (the PID slot is occupied; the process is `<defunct>`),
  which on 2026-06-04 caused the alan crash test to wedge: SIGKILL'd claude
  became a zombie under tmux, the new wrapper's singleton check kept
  reporting "launcher already alive (pid …)", systemd's `Restart=on-failure`
  looped without recovering, unit stayed `activating`. After this fix the
  check additionally inspects `ps -p $PID -o state=`; a `Z` head character is
  treated as stale and the file is cleaned up. Without this, the previous
  crash-survival fix (#184) couldn't actually complete a restart cycle on a
  SIGKILL'd claude, which is the dominant crash mode for a Claude Code
  session.
- ADR-0073 Step 1 crash-survival fix — `systemd/treadmill-channel-launch` gains
  a `STOPPED_BY_OPERATOR` flag flipped by a `trap '...' TERM INT` handler, and
  the trailing `while tmux has-session …` loop is followed by an explicit
  exit-status decision: SIGTERM/SIGINT (operator-initiated `systemctl stop`)
  exits 0 and leaves the unit stopped; any other tmux-ended cause exits 1 and
  systemd respawns per `Restart=on-failure` after `RestartSec=5s`. Empirical
  evidence from the 2026-06-04 carla crash test: `kill -9` of the claude PID
  closed the tmux pane (only one pane in the session), the wrapper's while-
  loop saw the session gone and exited via clean fall-through with code 0,
  and systemd did NOT restart — the substrate as shipped delivered reboot +
  SSH-drop + logout survival but not claude-crash survival. The wrapper now
  distinguishes the two exit causes. README's "Persistent sessions" section
  gains a one-paragraph "Crash recovery" note.
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
- `cc-relay.py` requires `TELEGRAM_CHAT_ID=<numeric>` in each target label's
  `telegram.env`. Retrieve it from the bot's chat history (open Telegram web,
  find the DM with the bot, the id is in the URL) and add it once per label.
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
