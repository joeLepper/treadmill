#!/usr/bin/env bash
# ADR-0104: bring up Gerald's persistent Codex session in tmux and keep the
# systemd unit alive while it exists. Gerald runs in an ISOLATED Codex home
# (CODEX_HOME=~/gerald/.codex) with its OWN app-server daemon that has no ChatGPT
# auth and only the opencode_go provider — structural open-weight enforcement.
set -uo pipefail
# $HOME/.asdf/shims must be on PATH: the isolated home's config.toml spawns the
# gerald_msg MCP server as `node ...msg-server.mjs`, and this daemon's env is what
# Codex resolves that command against. Without it, node is ENOENT, the MCP server
# never starts, and send_message/list_peers are silently absent from the session
# (matches the bridge-session PATH below).
export PATH="$HOME/.asdf/shims:$HOME/.local/bin:$PATH"
LABEL=gerald
GERALD_HOME="$HOME/gerald"
export CODEX_HOME="$GERALD_HOME/.codex"
UUID_FILE="$GERALD_HOME/.session-uuid"
SESS_DIR="$CODEX_HOME/sessions"
SECRET="$GERALD_HOME/secret.env"
command -v tmux >/dev/null 2>&1 || { echo "tmux required (ADR-0073)" >&2; exit 1; }

# Load the Go key into THIS script's env (exported) so children inherit it WITHOUT
# it appearing on any argv or in tmux send-keys text (review B2).
set -a; . "$SECRET"; set +a
: "${OPENCODE_API_KEY:?OPENCODE_API_KEY missing from secret.env}"

# Ensure the managed standalone binary is reachable from Gerald's isolated home
# (the daemon subcommand needs it). Share the binary via symlink; do not copy.
mkdir -p "$CODEX_HOME/packages"
[ -e "$CODEX_HOME/packages/standalone" ] || ln -sfn "$HOME/.codex/packages/standalone" "$CODEX_HOME/packages/standalone"

# Gerald's OWN daemon (isolated home). Inherits OPENCODE_API_KEY from env; never
# on argv. No OPENAI key. This daemon has no proprietary provider or credentials.
env -u OPENAI_API_KEY codex app-server daemon start >/dev/null 2>&1 || true

if ! tmux has-session -t "$LABEL" 2>/dev/null; then
  tmux new -d -s "$LABEL" -x 220 -y 50 -c "$GERALD_HOME"
fi

# Defaults (model + opencode_go provider) live in the isolated config.toml, so no
# --profile is needed. In-pane launcher sources the key from the file (review B2).
CFG='--dangerously-bypass-approvals-and-sandbox'
LAUNCH="exec env -u OPENAI_API_KEY CODEX_HOME=\"$CODEX_HOME\" sh -c 'set -a; . \"$SECRET\"; set +a; exec codex"

if [ -f "$UUID_FILE" ]; then
  uuid=$(cat "$UUID_FILE")
  tmux send-keys -t "$LABEL" "$LAUNCH resume $uuid $CFG'" Enter
else
  # Adopt the ONE new rollout this launch creates (review B3: never global mtime).
  before=$(ls "$SESS_DIR"/*/*/*/*.jsonl 2>/dev/null | xargs -r -n1 basename 2>/dev/null \
           | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | sort -u)
  tmux send-keys -t "$LABEL" "$LAUNCH $CFG \"You are Gerald, an open-weight Codex sibling. Acknowledge in one line, then wait for messages.\"'" Enter
  for _ in $(seq 1 30); do
    sleep 1
    after=$(ls "$SESS_DIR"/*/*/*/*.jsonl 2>/dev/null | xargs -r -n1 basename 2>/dev/null \
            | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | sort -u)
    new=$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after"))
    [ "$(printf '%s\n' "$new" | grep -c .)" = 1 ] && { echo "$new" > "$UUID_FILE"; break; }
  done
  # Never enter the liveness loop with an unpinned thread (review B3 hardening):
  # zero or multiple new rollouts is an unresolved bootstrap — fail so systemd
  # retries rather than running a session the bridge cannot address.
  [ -f "$UUID_FILE" ] || { echo "could not pin exactly one new Gerald rollout" >&2; exit 1; }
fi

# Foreground liveness loop. Intentional stop => SIGTERM (exit 143, whitelisted).
# Unexpected session loss => exit non-zero so Restart=on-failure recovers (B5).
while tmux has-session -t "$LABEL" 2>/dev/null; do sleep 5; done
echo "gerald tmux session '$LABEL' vanished unexpectedly" >&2
exit 1
