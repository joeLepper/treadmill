#!/usr/bin/env bash
# Deterministic check for rule:uv-lock-resolves.
#
# Runs uv lock --check to verify that all dependencies in pyproject.toml
# can be resolved cleanly. Exits 0 if resolution succeeds.
# Exits 1 (fail) if resolution fails (hallucinated package, impossible version, etc.).
#
# This catches hallucinated dependency names and version conflicts before merge.
#
# Usage:
#   tools/rule-checks/uv-lock-resolves/uv-lock-check.sh

set -euo pipefail

if uv lock --check >/dev/null 2>&1; then
    echo "rule:uv-lock-resolves :: pass (dependencies resolve cleanly)"
    exit 0
else
    echo "rule:uv-lock-resolves :: fail (uv lock check failed)" >&2
    exit 1
fi
