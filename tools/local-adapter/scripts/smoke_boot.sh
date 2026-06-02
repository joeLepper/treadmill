#!/usr/bin/env bash
# tools/local-adapter/scripts/smoke_boot.sh
#
# ADR-0065 Step 1 — boot wrapper for the autoscaler smoke gate.
#
# Brings up the minimal Treadmill service set on real Docker
# (fully-local mode: moto + API + supporting containers, no SSO),
# waits for the API to report healthy, then spawns one worker via the
# autoscaler's start_worker_once entry point. Prints a structured
# BOOT_READY / BOOT_FAILED marker the GitHub Actions workflow keys off.
#
# Intended for CI smoke use; operators bringing up a local stack
# interactively use `treadmill-local up` directly.

set -euo pipefail

PORT=8088
PROXY_ENABLED=true
TIMEOUT=300

usage() {
    cat <<'EOF'
Usage: smoke_boot.sh [--port PORT] [--proxy-enabled BOOL] [--timeout SECONDS]

Boots the minimal Treadmill service set (no autoscaler subprocess,
no scheduler, no observability stack) on the local Docker daemon and
spawns one worker via the autoscaler's start_worker_once. Prints
BOOT_READY on success or BOOT_FAILED on timeout.

Flags:
  --port PORT             Host port the API publishes on (default: 8088).
  --proxy-enabled BOOL    Sets TREADMILL_EGRESS_PROXY_ENABLED before the
                          boot subprocess fires (default: true).
  --timeout SECONDS       Seconds to wait for /healthz to return 200
                          (default: 300).
  -h, --help              Show this help and exit.

Environment overrides (used by tests):
  SMOKE_TREADMILL_CMD     Command in place of 'uv run treadmill-local'.
  SMOKE_PYTHON_CMD        Command in place of 'uv run python' for the
                          worker-spawn one-liner.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            PORT="$2"
            shift 2
            ;;
        --proxy-enabled)
            PROXY_ENABLED="$2"
            shift 2
            ;;
        --timeout)
            TIMEOUT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "smoke_boot.sh: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

export TREADMILL_EGRESS_PROXY_ENABLED="$PROXY_ENABLED"

# Fully-local boot talks to moto on localhost; the boto3 clients
# inside start_worker_once need the endpoint and the dummy
# credentials (CI runners have no AWS creds set). Mirrors the env the
# autoscaler subprocess receives when spawned by ``up``.
export AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://localhost:5001}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"

TREADMILL_CMD="${SMOKE_TREADMILL_CMD:-uv run treadmill-local}"
PYTHON_CMD="${SMOKE_PYTHON_CMD:-uv run python}"

echo "smoke_boot: starting treadmill-local up (port=${PORT}, proxy=${PROXY_ENABLED}, timeout=${TIMEOUT}s)"
# shellcheck disable=SC2086
${TREADMILL_CMD} up --no-build --no-autoscaler --no-scheduler --no-observability

HEALTHZ_URL="http://localhost:${PORT}/healthz"
echo "smoke_boot: polling ${HEALTHZ_URL} (up to ${TIMEOUT}s)"
deadline=$(( $(date +%s) + TIMEOUT ))
healthy=0
while (( $(date +%s) < deadline )); do
    if curl -sf -o /dev/null -m 5 "${HEALTHZ_URL}"; then
        healthy=1
        break
    fi
    sleep 2
done

if (( healthy == 0 )); then
    echo "BOOT_FAILED: API ${HEALTHZ_URL} did not return 200 within ${TIMEOUT}s"
    exit 1
fi

echo "smoke_boot: API healthy; spawning one worker via start_worker_once"
# shellcheck disable=SC2086
if ! ${PYTHON_CMD} -c "
from pathlib import Path
from treadmill_local.runtime import AGENT_FAMILY, LocalRuntime
runtime = LocalRuntime(infra_dir=Path('infra'), build_images=False)
container = runtime.start_worker_once(AGENT_FAMILY)
print(f'smoke_boot: spawned worker container={container.short_id} name={container.name}')
"; then
    echo "BOOT_FAILED: start_worker_once raised"
    exit 1
fi

echo "BOOT_READY"
exit 0
