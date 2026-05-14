#!/usr/bin/env bash
# Deterministic check for rule:cdk-synth-passes.
#
# Runs cdk synth -q in the infra/ directory to verify that the CDK app
# synthesizes cleanly without errors. Exits 0 if synthesis succeeds.
# Exits 1 (fail) if synthesis fails (import error, invalid construct, etc.).
#
# This catches hallucinated constructs, import errors, and token-shape mistakes
# before they break deployment.
#
# Usage:
#   tools/rule-checks/cdk-synth-passes/cdk-synth-check.sh

set -euo pipefail

# Change to infra directory and run cdk synth
cd infra || { echo "rule:cdk-synth-passes :: fail (infra/ directory not found)" >&2; exit 1; }

if uv run cdk synth -q >/dev/null 2>&1; then
    echo "rule:cdk-synth-passes :: pass (CDK synthesis succeeded)"
    exit 0
else
    echo "rule:cdk-synth-passes :: fail (cdk synth exited with error)" >&2
    exit 1
fi
