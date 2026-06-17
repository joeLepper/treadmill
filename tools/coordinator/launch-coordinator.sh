#!/usr/bin/env bash
# launch-coordinator.sh — one-command coordinator launch (ADR-0084 Phase 5).
#
# Reconciles the API's plan-id assignment into coordinator.env, then
# starts the specialized systemd unit. Idempotent on the env file: an
# existing TREADMILL_COORDINATOR_PLANS line is replaced; an absent file
# is created with TREADMILL_ROLE + TREADMILL_COORDINATOR_PLANS.
#
# Usage:
#   launch-coordinator.sh --repo <slug> --plan-id <uuid>
#
# Example:
#   launch-coordinator.sh --repo ramjac --plan-id 7b3e...c14a
#
# The systemd unit `treadmill-channel@coordinator-<slug>.service` must
# exist (one per repo). For an example instantiation, see the checked-in
# tools/cc-channels/systemd/treadmill-channel@coordinator-<slug>.service unit.
set -euo pipefail

REPO=""
PLAN_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="${2:?--repo requires a value}"; shift 2 ;;
    --plan-id)
      PLAN_ID="${2:?--plan-id requires a value}"; shift 2 ;;
    -h|--help)
      sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "[launch-coordinator] unknown argument: $1" >&2
      echo "usage: launch-coordinator.sh --repo <slug> --plan-id <uuid>" >&2
      exit 2 ;;
  esac
done

if [[ -z "$REPO" || -z "$PLAN_ID" ]]; then
  echo "usage: launch-coordinator.sh --repo <slug> --plan-id <uuid>" >&2
  exit 2
fi

TEAM_DIR="$HOME/.treadmill/teams/$REPO"
ENV_FILE="$TEAM_DIR/coordinator.env"
UNIT="treadmill-channel@coordinator-$REPO.service"

mkdir -p "$TEAM_DIR"

# Reconcile coordinator.env. Two cases:
#   (a) File exists with a TREADMILL_COORDINATOR_PLANS line — replace it
#       with the supplied --plan-id (preserves any other vars the API
#       wrote, e.g. TREADMILL_OPERATOR_INSTANCE).
#   (b) File missing or lacks the line — create / append with both
#       TREADMILL_ROLE=coordinator and the plan id.
if [[ -f "$ENV_FILE" ]] && grep -q '^TREADMILL_COORDINATOR_PLANS=' "$ENV_FILE"; then
  # In-place replace. Bracketed grouping with sed -i is portable.
  sed -i "s|^TREADMILL_COORDINATOR_PLANS=.*|TREADMILL_COORDINATOR_PLANS=$PLAN_ID|" \
    "$ENV_FILE"
  echo "[launch-coordinator] updated TREADMILL_COORDINATOR_PLANS in $ENV_FILE"
else
  {
    if [[ ! -f "$ENV_FILE" ]] || ! grep -q '^TREADMILL_ROLE=' "$ENV_FILE"; then
      echo "TREADMILL_ROLE=coordinator"
    fi
    echo "TREADMILL_COORDINATOR_PLANS=$PLAN_ID"
  } >> "$ENV_FILE"
  echo "[launch-coordinator] wrote $ENV_FILE"
fi

# Hand off to systemd. ``systemctl --user start`` is idempotent on an
# already-active unit (no-op); on a stopped unit it starts. Reload-or-
# restart would be needed if the unit fileitself changed — that's the
# operator's responsibility when editing the unit, not this launcher's.
echo "[launch-coordinator] starting $UNIT"
systemctl --user start "$UNIT"

echo "[launch-coordinator] coordinator session is now under systemd; attach via"
echo "[launch-coordinator]   tmux attach -t coordinator-$REPO"
