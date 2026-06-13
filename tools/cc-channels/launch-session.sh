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

# ── per-label team-role detection (ADR-0084 §3A; ADR-0087 §Team bootstrap) ──
# Labels of the form `<role>-<repo-slug>[-N]` mark a Treadmill team session:
#   coordinator-<slug>     — single PM session for the repo
#   evaluator-<slug>       — single PR-audit session for the repo (ADR-0087)
#   worker-<slug>-N        — implementer session N for the repo (ADR-0087)
#
# All three roles pin workdir to the per-label subdir
#   ~/.treadmill/teams/<repo-slug>/<label>/
# so Claude Code auto-discovers the rendered CLAUDE.md from cwd AND the
# rendered .claude/settings.json (which registers the worker's PostToolUse
# relay-inject hook). Prior to ADR-0087 PR-H, the coordinator pinned to
# the TEAM dir (one level up) and evaluator + worker sessions ran from the
# default workdir entirely. Both meant install_team()-rendered per-label
# files were never read — the stale root <team>/CLAUDE.md (or no CLAUDE.md
# at all) won instead, and the PostToolUse hook never registered. Per-label
# cwd lands every session at the files install_team() wrote for it. See
# docs/learnings/2026-06-10-template-install-layout-vs-launcher-cwd-mismatch.md.
#
# Coordinator additionally sources <team>/coordinator.env. The env file
# stays at the team root (not per-label) so the API's plan-id write-through
# (ADR-0084 §3A) keeps working without per-label awareness. The source
# MUST happen before the cwd is rewritten so any relative paths in the env
# file resolve correctly (defensive — the current contract is absolute-only).
#
# All three role classes also source the per-label <label>.env file written
# by `treadmill team up` (ADR-0087 PR-B). It carries TREADMILL_ROLE +
# TREADMILL_LABEL + TREADMILL_API_URL.
#
# Coordinators skip the dispatch-reminder print further below — they do
# not dispatch their own work, they route signals for other workers' work.

_TREADMILL_ROLE=""
_REPO_SLUG=""
if [[ "$LABEL" == coordinator-* ]]; then
  _TREADMILL_ROLE="coordinator"
  _REPO_SLUG="${LABEL#coordinator-}"
elif [[ "$LABEL" == evaluator-* ]]; then
  _TREADMILL_ROLE="evaluator"
  _REPO_SLUG="${LABEL#evaluator-}"
elif [[ "$LABEL" =~ ^worker-(.+)-[0-9]+$ ]]; then
  _TREADMILL_ROLE="worker"
  _REPO_SLUG="${BASH_REMATCH[1]}"
else
  # Non-team labels (treadmill-alan, treadmill-bert, ...) are orchestrator
  # sessions. Setting the role here is what arms the wake-class filter's
  # role default in treadmill-events.ts — without it the filter ships but
  # never applies (the 2026-06-11 integration gap: every orchestrator kept
  # waking on check_run_completed noise post-merge). The same TREADMILL_ROLE
  # export below arms the ADR-0090 coordinator + evaluator defaults
  # (task fe98030f) for team labels, whose role comes from their .env files.
  _TREADMILL_ROLE="orchestrator"
fi

if [[ -n "$_TREADMILL_ROLE" && "$_TREADMILL_ROLE" != "orchestrator" ]]; then
  _TEAM_DIR="$HOME/.treadmill/teams/$_REPO_SLUG"
  _SESSION_DIR="$_TEAM_DIR/$LABEL"
  mkdir -p "$_SESSION_DIR"

  # Source coordinator.env from the TEAM root BEFORE relocating cwd — the
  # env file's path predates per-label dirs; relocating it would break the
  # API's plan-assignment write-through (ADR-0084 §3A). Only coordinators
  # source this file today; evaluator + worker labels carry their config
  # in the per-label <label>.env source'd below.
  if [[ "$_TREADMILL_ROLE" == "coordinator" ]]; then
    _COORD_ENV="$_TEAM_DIR/coordinator.env"
    if [[ -f "$_COORD_ENV" ]]; then
      # `set -a` auto-exports every variable assigned during the source so
      # bare ``KEY=value`` lines in coordinator.env (the API-written form
      # — no need for ``export`` per line) reach the spawned claude process.
      set -a
      # shellcheck disable=SC1090
      source "$_COORD_ENV"
      set +a
    fi
  fi

  # Source the per-label <label>.env file written by `treadmill team up`.
  # Sourced AFTER coordinator.env, so on overlapping keys the per-label
  # file wins (later source overwrites). Intentional: the per-label file
  # is the ADR-0087-era config surface; coordinator.env persists only for
  # the API's plan-id write-through.
  _LABEL_ENV="$_SESSION_DIR/$LABEL.env"
  if [[ -f "$_LABEL_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$_LABEL_ENV"
    set +a
  fi

  if [[ "$WORKDIR" != "$_SESSION_DIR" && "$WORKDIR" != "$PWD" ]]; then
    echo "[launch-session] $_TREADMILL_ROLE label — overriding workdir '$WORKDIR' with session dir '$_SESSION_DIR'" >&2
  fi
  WORKDIR="$_SESSION_DIR"
fi

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
#
# The trailing `|| true` is load-bearing under `set -euo pipefail`: a dead
# PID makes `ps -p` exit non-zero, pipefail propagates it, and the failing
# command-substitution trips `set -e` — without `|| true`, the launcher
# silently exits 1 on every stale-pidfile case. (Caught by the same alan
# crash test that exposed the zombie-PID bug.)
PIDFILE="$STATE_ROOT/launcher.pid"
if [[ -f "$PIDFILE" ]]; then
  _pid=$(cat "$PIDFILE")
  _state=$(ps -p "$_pid" -o state= 2>/dev/null | head -c 1 || true)
  if [[ -n "$_pid" ]] && kill -0 "$_pid" 2>/dev/null && [[ "$_state" != "Z" ]]; then
    echo "[launch-session] launcher already alive for label $LABEL (pid $_pid); refusing to start" >&2
    exit 1
  fi
  rm -f "$PIDFILE"
fi

# Suppress claude's "Resuming the full session will consume a substantial
# portion of your usage limits" prompt for supervised launches. The prompt
# fires when both the recorded transcript age (default 70 min) and the
# token count (default 100k) exceed claude's internal thresholds — common
# for the long-lived sessions this launcher targets. Without this, every
# supervised restart of a mature session wedges on a prompt the operator
# isn't there to dismiss. Manual `claude` invocations are unaffected;
# these overrides only enter the supervised process tree.
# Reverse-engineered from claude 2.1.165 (`Rw9` in the bundled JS); the
# in-binary env-var lookups are stable across recent patches.
export CLAUDE_CODE_RESUME_THRESHOLD_MINUTES=999999
export CLAUDE_CODE_RESUME_TOKEN_THRESHOLD=999999999

# ── treadmill-events channel (ADR-0068) ─────────────────────────────────────
export TREADMILL_SESSION_LABEL="$LABEL"
# Role for the ADR-0089 wake-class filter. Team labels already carry
# TREADMILL_ROLE via their sourced .env files; this export covers the
# orchestrator labels (derived above) and never overrides a preset env.
export TREADMILL_ROLE="${TREADMILL_ROLE:-$_TREADMILL_ROLE}"
# Direct API port — the :8080 auth proxy does not upgrade WebSockets.
export TREADMILL_API_URL="${TREADMILL_API_URL:-http://localhost:8088}"
# Per-session relay verbosity (ADR-0071): quiet | normal | verbose.
# Default quiet — merges + ADR-0062 unexpected-terminal escalations only.
export TREADMILL_RELAY_LEVEL="${TREADMILL_RELAY_LEVEL:-quiet}"

# Per-role model pin (task 5d14fbcc — INCIDENT 2026-06-12): model-less
# sessions default to claude-fable-5 (unavailable) on --resume. Team
# roles (coordinator, evaluator, worker) get ANTHROPIC_MODEL from their
# per-label .env already sourced above (written by `treadmill team up`).
# Orchestrators carry no per-label .env, so this fallback is their pin.
# The ${...:-} form never overrides an already-set value, so re-rendered
# team .env files (e.g. worker=sonnet) are respected without conflict.
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-opus-4-8}"

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
if [[ "${TREADMILL_ROLE:-}" == "coordinator" ]]; then
  echo "[launch-session] role=coordinator — skipping dispatch reminder; coordinator routes signals, does not dispatch" >&2
else
  echo "[launch-session] reminder: dispatch with --created-by $LABEL" >&2
fi
echo "[launch-session] permissions BYPASSED — set the sender allowlist before relying on the bot:" >&2
echo "[launch-session]   DM the bot, then:  $_HERE/cc-access.py --label $LABEL pair <code>" >&2
echo "[launch-session]   then lock it down:  $_HERE/cc-access.py --label $LABEL policy allowlist" >&2
echo "[launch-session]   (use cc-access.py, NOT /telegram:access — the stock skill targets the wrong state dir under per-bot isolation)" >&2

cd "$WORKDIR"
# Persist the resolved workdir so the systemd wrapper can re-create tmux at
# the right cwd after a crash. `claude --resume <session-id>` binds to
# ~/.claude/projects/<cwd-slug>/<session-id>.jsonl — wrong cwd means the
# transcript can't be found and claude opens a fresh trust-prompt session.
# The supervised unit runs from cwd=$HOME by default, so without this file
# the wrapper's `tmux new -d -s` inherits the wrong cwd. See
# `docs/learnings/2026-06-04-systemd-default-cwd-breaks-claude-resume.md`.
echo "$WORKDIR" > "$STATE_ROOT/workdir"
# `exec` replaces this shell with claude; the PID we record now stays valid
# for the lifetime of the claude process. We do not register an EXIT trap to
# unlink the file (it would not fire across exec) — stale entries are detected
# on next start via the kill -0 check above.
echo $$ > "$PIDFILE"
# Per-session Claude account (task b561910d): resolves CLAUDE_CONFIG_DIR
# from $STATE_ROOT/claude-account when set — the limit-park recovery's
# failover surface. Default stays ~/.claude.
# shellcheck disable=SC1091
source "$_HERE/claude-account-env.sh"
exec claude "${CHANNEL_ARGS[@]}" "${SESSION_ARGS[@]}" "$@"
