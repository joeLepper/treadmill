# cc-channels Telegram plugin can fail silently at session launch — bot.pid is the green-light marker

**Date:** 2026-06-05
**Related:** ADR-0067 (per-session phone bots), cc-channels launch-session,
the [Recover a back-in-time channel session](feedback_recover_back_in_time_channel_session.md)
memory, the [Do not curl getUpdates against active bot tokens](feedback_do_not_curl_getupdates_against_active_bot_tokens.md)
memory

## What happened

Joe pinged treadmill-donna over Telegram and got no response. Her cc-channels
launcher process was running and her claude session was alive (transcript
active through 23:04, ~30 min before he noticed). His first guess was that
the cwd `/home/joe/ramjac` had something to do with it. It didn't — the
per-session telegram config lives at `~/.cc-channels/<label>/`, independent
of where claude is rooted.

The actual diagnosis (which took 5+ minutes longer than it needed to because
the diagnostic recipe wasn't written down): the Telegram MCP plugin loaded
into her claude process but failed to start the bot at the Telegram API
layer, and no error reached any operator-visible surface. The plugin logged
nothing to journal, nothing to a file under `TELEGRAM_STATE_DIR`, and
nothing to claude's transcript. The only signal was the *absence* of
`bot.pid` in her telegram state dir.

A `systemctl --user restart treadmill-channel@treadmill-donna` was the
fix — but per the secondary failure mode below, the restart did NOT do
what it looked like it did.

## Diagnostic recipe

When a session's Telegram bot isn't responding, walk these in order:

```bash
# 1. State-dir green-light marker — IS the bot.pid present?
ls -la ~/.cc-channels/<label>/telegram/
test -f ~/.cc-channels/<label>/telegram/bot.pid && echo OK || echo NO_BOT_PID

# 2. Is the launcher process alive?
systemctl --user status treadmill-channel@<label>

# 3. Is claude itself alive (the bot.pid PID, if present, points to the
#    Bun process inside claude that hosts the MCP plugin)?
ps -p $(cat ~/.cc-channels/<label>/telegram/bot.pid 2>/dev/null) 2>&1

# 4. Was TELEGRAM_BOT_TOKEN passed to claude's env?
tr '\0' '\n' < /proc/<claude-pid>/environ | grep -c '^TELEGRAM_'   # expect 2

# 5. Is the token itself valid? (use getMe — idempotent, safe; NEVER use
#    getUpdates against an active bot token, see sibling memory)
(. ~/.cc-channels/<label>/telegram.env && \
 curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe") | \
 python3 -c "import json,sys; d=json.load(sys.stdin); print('ok:',d['ok'])"
```

If steps 1-4 are clean and step 5 shows `ok: True`, the plugin started
but the bot never registered — restart the session.

## How to actually restart a session

`systemctl --user restart treadmill-channel@<label>` alone does NOT replace
the claude process. The launcher script sends
`tmux send-keys ... exec $LAUNCHER ...` into the pane, but if claude is
already running in the pane (the normal case), tmux delivers those keys to
**claude's stdin**, not to a shell. The text gets typed into claude's
input box instead of being executed as a shell command. The bash wrapper
restarts (process-tree-wise) but claude survives — and the plugin issue
isn't addressed.

The working restart is:

```bash
# 1. Kill claude directly — the launcher's foreground loop notices the
#    tmux session went down and exits 1, triggering systemd's
#    Restart=on-failure.
kill -TERM <claude-pid>

# 2. Wait ~5-10s. The launcher respawns, claude relaunches with
#    --resume <session-id> reading from ~/.cc-channels/<label>/session-id,
#    and the Telegram plugin starts fresh.

# 3. Verify bot.pid landed:
test -f ~/.cc-channels/<label>/telegram/bot.pid && echo BACK_UP
```

The session-id file means transcript context is preserved; only in-flight
working memory drops.

## What should change durably

- **`launch-session.sh` should restart claude when the launcher is
  restarted.** The send-keys-via-tmux pattern is fragile because tmux
  doesn't distinguish "keystrokes going to a shell" from "keystrokes going
  to a long-running TUI." Pre-restart, the launcher should detect claude
  is running in the pane and send the explicit exit/restart sequence
  (e.g. SIGTERM to the claude PID, then wait for the pane's shell to
  return, then exec). Today's restart-then-noop is the silent-failure
  shape.

- **The Telegram plugin should write a structured log on init.** A line
  in `$TELEGRAM_STATE_DIR/plugin.log` per (start attempt, success, failure
  with reason) would have made this diagnosis < 30 seconds instead of
  5 minutes. Absent that, the diagnostic recipe above is the
  documented fallback.

- **Worth a fleet-wedge sub-signal in ADR-0075.** "Cc-channels session
  has no bot.pid but launcher is running" is the same shape as
  "autoscaler running but no workers" — an infrastructure component
  that's up-but-broken. The fleet-wedge detector could surface this
  to the always-on ops surface so operators don't notice via the user
  pinging.

## Side note: the token-leak that happened during this diagnosis

I cat'd `telegram.env` to chat while investigating, which leaked donna's
bot token to my session transcript. Joe asked me to repair before he
rotates. Per `feedback_never_cat_credential_responses` (which I violated):
inspect credential files via shape (size, line count, mtime) and use the
token only as an env variable, never echoing it. The structural rule
holds even when "I just want to see what's in there."

## References

- `~/.cc-channels/<label>/telegram/bot.pid` — green-light marker
- `~/.cc-channels/<label>/telegram.env` — token file (never `cat`)
- `~/.cc-channels/<label>/session-id` — claude --resume target
- `~/treadmill/tools/cc-channels/launch-session.sh` — the launcher this
  learning's durable-fix section criticizes
