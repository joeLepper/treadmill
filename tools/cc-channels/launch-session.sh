#!/usr/bin/env bash
# launch-session.sh — start a labeled Claude Code session with its channels
# (ADR-0067 phone access + ADR-0068 treadmill events; shared conventions).
#
# The <label> is the session identity primitive (ADR-0068 Part 1):
#   * names the session's Telegram bot (the phone's chat list = session list)
#   * keys the channel state dirs under ~/.cc-channels/<label>/
#   * is the value the session MUST pass as `--created-by <label>` on every
#     `treadmill plan submit`, so the events channel receives its own work.
#
# Usage:
#   launch-session.sh <label> [workdir] [-- <extra claude args>]
#
# Telegram (optional per session): put the bot's token in
#   ~/.cc-channels/<label>/telegram.env   as   TELEGRAM_BOT_TOKEN=...
# When absent, the session launches with the treadmill-events channel only.
#
# One-time setup (see tools/cc-channel-treadmill/README.md):
#   * bun installed; `bun install` run in tools/cc-channel-treadmill/
#   * "treadmill-events" registered in ~/.claude.json mcpServers (absolute path)
#   * telegram plugin installed + paired, sender allowlist configured FIRST
#     (bypassed-permission sessions: ungated inbound = prompt injection).
#
# Pinned against: Claude Code 2.1.161. Channels are a research preview —
# re-verify the --channels / --dangerously-load-development-channels contract
# after CC upgrades (ADR-0067/0068 watch-out).
set -euo pipefail

LABEL="${1:?usage: launch-session.sh <label> [workdir] [-- extra claude args]}"
shift
WORKDIR="$PWD"
if [[ $# -gt 0 && "$1" != "--" ]]; then
  WORKDIR="$1"; shift
fi
[[ "${1:-}" == "--" ]] && shift

STATE_ROOT="$HOME/.cc-channels/$LABEL"
mkdir -p "$STATE_ROOT/telegram" "$STATE_ROOT/treadmill"

# Single-instance contract per ADR-0073: refuse to start if another launcher is
# alive for this label. Belt-and-suspenders with the systemd wrapper's check;
# this layer guards against an operator bypassing the wrapper with a direct
# invocation. We use kill -0 to distinguish a live PID from a stale file (e.g.
# left over from a power-cut); a stale file is silently cleaned up.
#
# `kill -0` reports success on zombies — we additionally check the process
# state so a `<defunct>` claude left over from a SIGKILL doesn't block the
# next launch. See 2026-06-04 alan crash test (systemd wrapper failed to
# recover for the same reason).
PIDFILE="$STATE_ROOT/launcher.pid"
if [[ -f "$PIDFILE" ]]; then
  _pid=$(cat "$PIDFILE")
  _state=$(ps -p "$_pid" -o state= 2>/dev/null | head -c 1)
  if [[ -n "$_pid" ]] && kill -0 "$_pid" 2>/dev/null && [[ "$_state" != "Z" ]]; then
    echo "[launch-session] launcher already alive for label $LABEL (pid $_pid); refusing to start" >&2
    exit 1
  fi
  rm -f "$PIDFILE"
fi

# ── treadmill-events channel (ADR-0068) ─────────────────────────────────────
export TREADMILL_SESSION_LABEL="$LABEL"
# Direct API port — the :8080 auth proxy does not upgrade WebSockets.
export TREADMILL_API_URL="${TREADMILL_API_URL:-http://localhost:8088}"
# Per-session relay verbosity (ADR-0071): quiet | normal | verbose.
# Default quiet — merges + ADR-0062 unexpected-terminal escalations only.
export TREADMILL_RELAY_LEVEL="${TREADMILL_RELAY_LEVEL:-quiet}"

# ── telegram channel (ADR-0067), only when this label has a bot ─────────────
CHANNEL_ARGS=()
TELEGRAM_ENV="$STATE_ROOT/telegram.env"
if [[ -f "$TELEGRAM_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$TELEGRAM_ENV"          # provides TELEGRAM_BOT_TOKEN
  export TELEGRAM_BOT_TOKEN
  export TELEGRAM_STATE_DIR="$STATE_ROOT/telegram"
  CHANNEL_ARGS+=(--channels plugin:telegram@claude-plugins-official)
else
  echo "[launch-session] no $TELEGRAM_ENV — starting without the telegram channel" >&2
fi

# Custom channels stay behind the development flag during the research
# preview (per-entry bypass; --channels entries are NOT covered by it).
CHANNEL_ARGS+=(--dangerously-load-development-channels server:treadmill-events)

# Bypass permission prompts (ADR-0067): these are long-lived, away-from-keyboard
# sessions driven from the phone — a permission prompt nobody is at the terminal
# to answer would stall the session indefinitely. This is why the inbound
# sender-allowlist gate (telegram :access policy allowlist) is MANDATORY, not
# optional: with permissions bypassed, an ungated channel message is direct
# code-execution. Pair + allowlist each bot before relying on it.
CHANNEL_ARGS+=(--dangerously-skip-permissions)

# Per-label persistent session: one stable Claude Code session per label, so
# `launch-session.sh <label>` always lands back in that label's own session
# without the operator passing --resume. We mint a session id on first launch
# and record it under the label's state dir; later launches resume it. Skip
# this entirely if the operator passed their own --resume/--continue/
# --session-id in the extra args (their flag wins).
SESSION_FILE="$STATE_ROOT/session-id"
_user_set_session=false
for _a in "$@"; do
  case "$_a" in -r|--resume|-c|--continue|--session-id) _user_set_session=true ;; esac
done
SESSION_ARGS=()
if ! $_user_set_session; then
  if [[ -f "$SESSION_FILE" ]]; then
    SESSION_ARGS=(--resume "$(cat "$SESSION_FILE")")
  else
    _sid="$(python3 -c 'import uuid; print(uuid.uuid4())')"
    echo "$_sid" > "$SESSION_FILE"
    SESSION_ARGS=(--session-id "$_sid")
    echo "[launch-session] minted new session id for '$LABEL' (recorded in $SESSION_FILE)" >&2
  fi
fi

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[launch-session] label=$LABEL workdir=$WORKDIR channels=${CHANNEL_ARGS[*]}" >&2
echo "[launch-session] reminder: dispatch with --created-by $LABEL" >&2
echo "[launch-session] permissions BYPASSED — set the sender allowlist before relying on the bot:" >&2
echo "[launch-session]   DM the bot, then:  $_HERE/cc-access.py --label $LABEL pair <code>" >&2
echo "[launch-session]   then lock it down:  $_HERE/cc-access.py --label $LABEL policy allowlist" >&2
echo "[launch-session]   (use cc-access.py, NOT /telegram:access — the stock skill targets the wrong state dir under per-bot isolation)" >&2

cd "$WORKDIR"
# `exec` replaces this shell with claude; the PID we record now stays valid
# for the lifetime of the claude process. We do not register an EXIT trap to
# unlink the file (it would not fire across exec) — stale entries are detected
# on next start via the kill -0 check above.
echo $$ > "$PIDFILE"
exec claude "${CHANNEL_ARGS[@]}" "${SESSION_ARGS[@]}" "$@"
