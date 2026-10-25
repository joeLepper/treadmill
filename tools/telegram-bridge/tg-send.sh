#!/usr/bin/env bash
# tg-send.sh — outbound Telegram for a bridged session (ADR-0106).
#
# When the per-session Telegram MCP is OFF (the bridge is the sole getUpdates
# poller, so the MCP must not run a second poller on the same token), a session
# sends to the operator with a DIRECT Bot API sendMessage. This is that path.
#
# The bot token is read from ~/.cc-channels/<label>/telegram.env (mode 0600) and
# is NEVER printed: it goes into a variable and then into the request only. Do
# not run this under `set -x`.
#
# Usage:
#   tg-send.sh <label> <text> [chat_id]
#     <label>    session label, e.g. treadmill-alan
#     <text>     message body (quote it)
#     [chat_id]  defaults to Joe's chat (8956818786)
#
# Exit: 0 and prints {"ok":true,...} on success; non-zero on failure.
set -euo pipefail

LABEL="${1:?usage: tg-send.sh <label> <text> [chat_id]}"
TEXT="${2:?usage: tg-send.sh <label> <text> [chat_id]}"
CHAT="${3:-8956818786}"

ENVF="${HOME}/.cc-channels/${LABEL}/telegram.env"
[ -r "$ENVF" ] || { echo "no readable telegram.env for ${LABEL} at ${ENVF}" >&2; exit 2; }

# Parse TELEGRAM_BOT_TOKEN the same way bridge/config.mjs does: strip optional
# `export`, trim, drop surrounding quotes, and for an unquoted value take up to
# the first whitespace (so a trailing comment never leaks into the token).
TOKEN=$(grep -E '^[[:space:]]*(export[[:space:]]+)?TELEGRAM_BOT_TOKEN[[:space:]]*=' "$ENVF" \
  | head -1 \
  | sed -E 's/^[[:space:]]*(export[[:space:]]+)?TELEGRAM_BOT_TOKEN[[:space:]]*=[[:space:]]*//' \
  | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/' \
  | awk '{print $1}')
[ -n "$TOKEN" ] || { echo "no TELEGRAM_BOT_TOKEN in ${ENVF}" >&2; exit 2; }

# The sendMessage response contains no secret, so it is safe to print.
curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT}" \
  --data-urlencode "text=${TEXT}"
echo
unset TOKEN
