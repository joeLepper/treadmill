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
  elif [[ -n "$_ACCOUNT" ]]; then
    echo "[launch-session] WARNING: claude-account '$_ACCOUNT' set but ~/.claude-$_ACCOUNT missing — using default ~/.claude" >&2
  fi
fi
