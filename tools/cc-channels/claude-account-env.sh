#!/usr/bin/env bash
# Sourceable helper: per-session Claude account selection (task
# b561910d prerequisite — ADR-0055/0066 lineage). When
# $STATE_ROOT/claude-account names an account, point claude at
# ~/.claude-<account> via CLAUDE_CONFIG_DIR; otherwise the default
# ~/.claude applies. The limit-park recovery swaps the file and
# bounces — the relaunch lands here and picks the fallback up.
#
# Expects: STATE_ROOT and LABEL set by the sourcing launcher.

_ACCOUNT_FILE="$STATE_ROOT/claude-account"
if [[ -f "$_ACCOUNT_FILE" ]]; then
  _ACCOUNT=$(<"$_ACCOUNT_FILE")
  _ACCOUNT=${_ACCOUNT//[$'\t\r\n ']/}
  if [[ -n "$_ACCOUNT" && -d "$HOME/.claude-$_ACCOUNT" ]]; then
    export CLAUDE_CONFIG_DIR="$HOME/.claude-$_ACCOUNT"
    echo "[launch-session] $LABEL using claude account '$_ACCOUNT' (CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR)" >&2
    # The launcher always runs claude with --dangerously-skip-permissions, which
    # pops a one-time "Bypass Permissions mode" ACCEPTANCE modal on any config
    # dir that has never accepted it. An unattended session has no one to press
    # "2. Yes, I accept", so it wedges on stdin BEFORE loading the treadmill-events
    # channel — no crash, no MCP log, just blocked. A fresh leased dir hits this
    # on every launch (2026-06-16: this is exactly why the zephyr + ramjac-events
    # leased teams never came up — the latent parallel-teams bug). Seed the accept
    # flag into the leased dir's settings.json idempotently so the modal never
    # appears. Best-effort: a write failure or unparseable file must NOT break the
    # launch (the poller in treadmill-channel-launch is the backstop).
    _SETTINGS="$CLAUDE_CONFIG_DIR/settings.json"
    python3 - "$_SETTINGS" <<'PYEOF' || echo "[launch-session] WARNING: could not seed skipDangerousModePermissionPrompt into $_SETTINGS" >&2
import json, os, sys
p = sys.argv[1]
try:
    d = json.load(open(p)) if (os.path.exists(p) and os.path.getsize(p)) else {}
except Exception:
    raise SystemExit(1)  # never clobber an unparseable settings file
if not isinstance(d, dict):
    raise SystemExit(1)
if d.get("skipDangerousModePermissionPrompt") is not True:
    d["skipDangerousModePermissionPrompt"] = True
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, p)
PYEOF
  elif [[ -n "$_ACCOUNT" ]]; then
    echo "[launch-session] WARNING: claude-account '$_ACCOUNT' set but ~/.claude-$_ACCOUNT missing — using default ~/.claude" >&2
  fi
fi
