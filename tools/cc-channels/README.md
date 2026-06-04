# tools/cc-channels — per-session Claude Code channel launcher

Launcher and access tooling for labelled Claude Code sessions that combine:
- **treadmill-events channel** (ADR-0068) — push-based dispatch lifecycle events
- **Telegram channel** (ADR-0067) — phone access and operator relay
- **Persistent sessions** (ADR-0073) — systemd-user + tmux supervision + `cc-attach`

## Quick start

```bash
# One-time setup: see "Persistent sessions" section below.

# Start a supervised session for label "bert"
systemctl --user enable --now treadmill-channel@bert.service

# Attach to it
cc-attach bert           # Ctrl-b d to detach

# Dispatch work from the session (use the same label as --created-by)
treadmill plan submit --created-by bert ...
```

## Direct launch (no systemd)

```bash
# Foreground — exits when Claude exits
tools/cc-channels/launch-session.sh bert [workdir] [-- extra claude args]
```

The label (`bert`) is the session identity primitive (ADR-0068): it names the
Telegram bot, keys the state directory `~/.cc-channels/bert/`, and must match
`--created-by bert` on every `treadmill plan submit`.

## Persistent sessions (ADR-0073)

### One-time setup

```bash
# Allow user services to survive after logout
loginctl enable-linger $USER

# Install the systemd unit
mkdir -p ~/.config/systemd/user
cp tools/cc-channels/systemd/treadmill-channel@.service ~/.config/systemd/user/
systemctl --user daemon-reload

# Install cc-attach to PATH
sudo install -m 0755 tools/cc-channels/bin/cc-attach /usr/local/bin/
# or without sudo, into a user-owned bin dir that's on PATH:
# cp tools/cc-channels/bin/cc-attach ~/bin/
```

The systemd unit assumes the repo is cloned at `~/treadmill`. If it's elsewhere,
add a drop-in before enabling the unit:

```bash
mkdir -p ~/.config/systemd/user/treadmill-channel@.service.d/
cat > ~/.config/systemd/user/treadmill-channel@.service.d/override.conf <<EOF
[Service]
Environment=TREADMILL_REPO_DIR=/path/to/your/treadmill
EOF
systemctl --user daemon-reload
```

### Per-label session management

```bash
# Enable + start
systemctl --user enable --now treadmill-channel@bert.service

# Attach (interactive)
cc-attach bert

# Detach without stopping
Ctrl-b d

# Check status / logs
systemctl --user status treadmill-channel@bert.service
journalctl --user -u treadmill-channel@bert.service -f

# Stop (does NOT destroy the session history; Claude exits cleanly)
systemctl --user stop treadmill-channel@bert.service

# Disable (won't start at boot)
systemctl --user disable treadmill-channel@bert.service
```

### Session lifecycle

```
systemd supervises treadmill-channel-launch
  └── treadmill-channel-launch keeps tmux session alive (sleeps in a loop)
        └── tmux session "bert"
              └── launch-session.sh --resume-by-label bert
                    └── claude --channels treadmill-events [telegram] --resume <id>
```

When Claude exits (or crashes):
1. `launch-session.sh` removes `~/.cc-channels/bert/launcher.pid` (EXIT trap).
2. The tmux session may exit, causing `treadmill-channel-launch` to exit.
3. systemd restarts `treadmill-channel-launch` after 5 s.
4. The new launch resumes the same per-label session ID (stored in
   `~/.cc-channels/bert/session-id`).

### Single-instance guard

Both `treadmill-channel-launch` and `launch-session.sh` check
`~/.cc-channels/<label>/launcher.pid`. If a live process holds the file, the
new invocation refuses to start. This prevents duplicate sessions from:
- a systemd restart storm while Claude is still running in the tmux window; or
- an operator invoking `launch-session.sh` directly while the supervised path is active.

## Telegram setup (optional per label)

```bash
# Create the bot token file
echo "TELEGRAM_BOT_TOKEN=<token>" > ~/.cc-channels/bert/telegram.env

# After starting the session, pair and lock the bot
DM the bot → get the pairing code
tools/cc-channels/cc-access.py --label bert pair <code>
tools/cc-channels/cc-access.py --label bert policy allowlist

# Use cc-access.py, NOT /telegram:access — the stock skill targets the wrong
# state dir under per-bot isolation (ADR-0067).
```

## Channel setup (treadmill-events)

One-time setup required before any session will receive lifecycle events:

1. Install [Bun](https://bun.sh).
2. In `tools/cc-channel-treadmill/`: `bun install`.
3. Register the server in `~/.claude.json` under `mcpServers` (absolute path):
   ```json
   "treadmill-events": {
     "command": "bun",
     "args": ["/home/<you>/treadmill/tools/cc-channel-treadmill/treadmill-events.ts"]
   }
   ```

See `tools/cc-channel-treadmill/README.md` for env vars and smoke test.

## Env vars

| Var | Default | Meaning |
| --- | --- | --- |
| `TREADMILL_SESSION_LABEL` | (set by launcher) | session label = `created_by` routing key |
| `TREADMILL_API_URL` | `http://localhost:8088` | Treadmill API — must be the direct port; `:8080` auth proxy doesn't upgrade WebSockets |
| `TREADMILL_REPO_DIR` | `$HOME/treadmill` | path to repo clone; used by the systemd unit |

## Troubleshooting

**`cc-attach bert` says "no session found"**
The systemd unit may not be running: `systemctl --user status treadmill-channel@bert.service`.
Check `journalctl --user -u treadmill-channel@bert.service` for errors.

**"launcher already running for label" on direct invocation**
A supervised session is active. Use `cc-attach bert` to join it, or stop the
unit first: `systemctl --user stop treadmill-channel@bert.service`.

**treadmill-events not connected in session**
Run `/mcp` inside the session; if `treadmill-events` shows "Failed to connect",
check `~/.claude/debug/<session-id>.txt` and verify `TREADMILL_API_URL`.

**systemd restarts in a loop**
If Claude exits immediately and the tmux session stays up, `treadmill-channel-launch`
won't exit (it loops on `tmux has-session`). This is correct; check the Claude
exit reason with `cc-attach bert` and inspect the session history.
