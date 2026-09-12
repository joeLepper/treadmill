#!/usr/bin/env bash
# ADR-0104 Path A: the Responses->Chat translation shim for Gerald.
# LiteLLM exposes an OpenAI Responses API that Codex speaks and translates it to
# OpenCode Go Chat Completions (Codex 0.154 dropped chat wire_api; Go's
# open-weight models are chat-only). Foreground; systemd keeps it alive.
set -uo pipefail
SHIM=/home/joe/gerald/shim
# The Go API key for upstream calls (shared with the session; lives at ~/gerald).
# Fail loud if it is missing or a placeholder.
set -a; . /home/joe/gerald/secret.env; set +a
# Exit 78 (EX_CONFIG) on a permanent config error so systemd stops retrying
# (RestartPreventExitStatus=78) instead of crash-looping every 5s (review B4).
case "${OPENCODE_API_KEY:-}" in
  ""|*REPLACE_WITH_GO_KEY*) echo "shim: OPENCODE_API_KEY missing/placeholder in secret.env" >&2; exit 78;;
esac
# Validate the config before exec so a malformed YAML fails loud once, not in a
# systemd crash-loop. LiteLLM's own YAML error is buried in a traceback.
"$SHIM/venv/bin/python" -c "import yaml,sys; yaml.safe_load(open('$SHIM/config.yaml'))" || {
  echo "shim: config.yaml is not valid YAML — refusing to start" >&2; exit 78; }
exec "$SHIM/venv/bin/litellm" --config "$SHIM/config.yaml" --host 127.0.0.1 --port 4141
