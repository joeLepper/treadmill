#!/usr/bin/env bash
# Deterministic check for rule:python-tests-resolve.
#
# Runs pytest --collect-only across the repo to verify that all test files
# are importable and discoverable. Exits 0 if collection succeeds.
# Exits 1 (fail) if collection fails (import error, missing dependency, etc.).
#
# This catches hallucinated imports and module-load failures before merge.
#
# Usage:
#   tools/rule-checks/python-tests-resolve/pytest-collect.sh

set -euo pipefail

# Try to run pytest on test discovery. If it fails for any reason
# (import error, missing module, etc.), the check fails.
if uv run pytest --collect-only -q 2>&1 | grep -E "(ERROR|FAILED|ImportError|ModuleNotFoundError)" >/dev/null; then
    echo "rule:python-tests-resolve :: fail (test collection errors detected)" >&2
    exit 1
fi

# If pytest itself fails (non-zero exit), that's also a fail.
if ! uv run pytest --collect-only -q >/dev/null 2>&1; then
    echo "rule:python-tests-resolve :: fail (pytest --collect-only exited with error)" >&2
    exit 1
fi

echo "rule:python-tests-resolve :: pass (test collection succeeded)"
exit 0
