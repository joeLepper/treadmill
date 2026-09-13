#!/usr/bin/env bash
# ADR-0107: the fleet model gateway. LiteLLM in CONFIG-mode fronting OpenCode Go
# OPEN-WEIGHT models only, from its OWN venv (no dependency on any sibling). The
# model_list in config.yaml IS the allowlist — a proprietary model is not listed,
# so it 404s and no Go budget is ever spent on gpt-*/grok-*.
set -uo pipefail
GW_HOME="$HOME/model-gateway"
SECRET="$GW_HOME/secret.env"
CONFIG="$GW_HOME/config.yaml"
PORT="${GATEWAY_PORT:-4250}"
LITELLM="$GW_HOME/venv/bin/litellm"
[ -x "$LITELLM" ] || { echo "gateway venv missing: $LITELLM" >&2; exit 78; }
[ -f "$SECRET" ] || { echo "secret.env missing: $SECRET" >&2; exit 78; }
[ -f "$CONFIG" ] || { echo "config.yaml missing: $CONFIG" >&2; exit 78; }
set -a; . "$SECRET"; set +a
: "${OPENCODE_API_KEY:?OPENCODE_API_KEY missing from secret.env}"
: "${GATEWAY_MASTER_KEY:?GATEWAY_MASTER_KEY missing from secret.env}"
# exec so systemd supervises litellm directly (Restart=on-failure).
exec "$LITELLM" --config "$CONFIG" --port "$PORT"
