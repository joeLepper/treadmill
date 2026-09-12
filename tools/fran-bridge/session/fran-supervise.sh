#!/usr/bin/env bash
# ADR-0102 unit 1: bring up Fran's persistent Codex session in tmux and keep
# the systemd unit alive while it exists. Mirrors the ADR-0073 substrate
# (treadmill-channel-launch). Resumes her pinned session for continuity;
# starts fresh + records the UUID on first run.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
LABEL=fran
FRAN_HOME="$HOME/fran"
UUID_FILE="$FRAN_HOME/.session-uuid"
SESS_DIR="$HOME/.codex/sessions"
command -v tmux >/dev/null 2>&1 || { echo "tmux required (ADR-0073)" >&2; exit 1; }

# The daemon must be up and CLEAN (env -u so its effective account is ChatGPT).
env -u OPENAI_API_KEY codex app-server daemon start >/dev/null 2>&1 || true

# Idempotent tmux session at Fran's durable cwd.
if ! tmux has-session -t "$LABEL" 2>/dev/null; then
  tmux new -d -s "$LABEL" -x 220 -y 50 -c "$FRAN_HOME"
fi

# Fran runs at fleet parity with the Claude Code siblings (operator directive
# 2026-09-11): approvals + Codex sandbox bypassed so she has equal autonomy
# (edit repos, self-register MCP tools). Host-level restrictions still apply;
# the auth guard (ExecStartPre) still enforces ChatGPT-only, no key.
CFG='--dangerously-bypass-approvals-and-sandbox'
if [ -f "$UUID_FILE" ]; then
  uuid=$(cat "$UUID_FILE")
  tmux send-keys -t "$LABEL" "exec env -u OPENAI_API_KEY codex resume $uuid $CFG" Enter
else
  tmux send-keys -t "$LABEL" "exec env -u OPENAI_API_KEY codex $CFG 'You are Fran. Acknowledge in one line, then wait for messages.'" Enter
  # Record the new session UUID so the next restart resumes THIS thread.
  for _ in $(seq 1 25); do
    sleep 1
    f=$(ls -t "$SESS_DIR"/*/*/*/*.jsonl 2>/dev/null | head -1)
    [ -z "$f" ] && continue
    u=$(basename "$f" .jsonl | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
    [ -n "$u" ] && { echo "$u" > "$UUID_FILE"; break; }
  done
fi

# Foreground liveness loop: keep the unit alive while the tmux session lives.
# Intentional stop => SIGTERM (exit 143, whitelisted). Unexpected session loss
# (e.g. a tmux-server churn) => exit non-zero so Restart=on-failure recovers Fran
# rather than leaving her down (matches the gerald-supervise fix).
while tmux has-session -t "$LABEL" 2>/dev/null; do sleep 5; done
echo "fran tmux session '$LABEL' vanished unexpectedly" >&2
exit 1
