#!/usr/bin/env bash
# Supervise the fran-bridge relay Claude Code session (ADR-0102). Keeps its tmux
# session alive and RE-KICKS it on each (re)start — a session restart kills the
# outbox Monitor, so the relay role must be re-armed every time. Mirrors the
# ADR-0073 supervise-in-tmux substrate (treadmill-channel-launch).
set -uo pipefail
export PATH="$HOME/.asdf/shims:$HOME/.local/bin:$PATH"
LABEL=cxbridge
WD=/home/joe/fran/bridge-session
command -v tmux >/dev/null 2>&1 || { echo "tmux required (ADR-0073)" >&2; exit 1; }

# The bridge is a STATELESS relay — it remembers nothing across restarts (it just
# re-arms the Monitor). Resuming the prior session-id adds a failure mode (a resume
# that exits kills the pane) with no benefit, so force a FRESH session each start.
rm -f "$HOME/.cc-channels/$LABEL/session-id" 2>/dev/null || true
tmux kill-session -t "$LABEL" 2>/dev/null || true
tmux new -d -s "$LABEL" -x 200 -y 50 -c "$WD"
tmux send-keys -t "$LABEL" "exec /home/joe/treadmill/tools/cc-channels/launch-session.sh $LABEL $WD" Enter

# Wait for readiness, answering the one-time folder-trust prompt; then re-kick.
kicked=0
for _ in $(seq 1 45); do
  sleep 2
  pane=$(tmux capture-pane -t "$LABEL" -p 2>/dev/null || true)
  case "$pane" in
    *"trust this folder"*|*"Do you trust"*)
      tmux send-keys -t "$LABEL" Down; sleep 1; tmux send-keys -t "$LABEL" Enter; continue ;;
  esac
  # CC idle prompt reached -> deliver the (short) re-kick once. Details live in CLAUDE.md.
  if [ "$kicked" -eq 0 ] && echo "$pane" | grep -qE 'bypass permissions on|Try "how does'; then
    tmux send-keys -t "$LABEL" "Read your CLAUDE.md and START your relay role now: arm the persistent outbox Monitor exactly as CLAUDE.md specifies, drain the outbox, then relay-only. Reply 'bridge ready' to alan."
    sleep 1; tmux send-keys -t "$LABEL" Enter
    kicked=1
    break
  fi
done

# Foreground liveness loop keeps the unit alive while the tmux session lives.
while tmux has-session -t "$LABEL" 2>/dev/null; do sleep 5; done
