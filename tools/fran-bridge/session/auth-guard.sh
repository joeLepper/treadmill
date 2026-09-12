#!/usr/bin/env bash
# ADR-0102 auth guard: Fran must run on the ChatGPT subscription, never the
# no-credit API key. Fails loud (blocks unit start) if the key could win.
# The daemon check is Fran's own review finding: `env -u` on the unit is not
# enough if the app-server DAEMON was started with the key — it then serves
# API-key mode regardless of the unit env, so we inspect the daemon's /proc.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
fail(){ echo "AUTH GUARD FAIL: $*" >&2; exit 1; }

# 1. The unit env must not carry the key.
[ -n "${OPENAI_API_KEY:-}" ] && fail "OPENAI_API_KEY is set in the unit env (would override ChatGPT auth)"

# 2. Stored auth must be ChatGPT.
codex login status 2>&1 | grep -qi "Logged in using ChatGPT" || fail "codex is not logged in via ChatGPT (run: codex login)"

# 3. The running app-server daemon must NOT have been started with the key.
pid=$(pgrep -u "$USER" -f 'app-server' | head -1 || true)
if [ -n "$pid" ] && [ -r "/proc/$pid/environ" ]; then
  if tr '\0' '\n' < "/proc/$pid/environ" | grep -q '^OPENAI_API_KEY='; then
    fail "app-server daemon (pid $pid) was started WITH OPENAI_API_KEY — restart clean: env -u OPENAI_API_KEY codex app-server daemon restart"
  fi
fi
echo "auth guard OK: ChatGPT auth, no key contamination"
exit 0
