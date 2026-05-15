#!/usr/bin/env bash
# Deterministic check for rule:python-tests-resolve.
#
# Runs ``pytest --collect-only`` in each Python project's directory to
# verify all test files are importable and discoverable. Treadmill has
# multiple Python projects (services/api, workers/agent,
# tools/local-adapter) — each with its own pyproject + tests dir —
# so we cannot run pytest from the repo root: there is no
# root-level pyproject and pytest finds nothing to collect against.
#
# Exits 0 if every project's collection succeeds (or the project has
# no tests). Exits 1 if any project's collection fails.
#
# This catches hallucinated imports + module-load failures before merge.

set -uo pipefail

PROJECTS=(
    services/api
    workers/agent
    tools/local-adapter
)

repo_root="$(cd "$(dirname "$0")/../../.." && pwd)"
failed_projects=()

for proj in "${PROJECTS[@]}"; do
    proj_dir="$repo_root/$proj"
    if [ ! -d "$proj_dir" ] || [ ! -d "$proj_dir/tests" ]; then
        # No project dir or no tests — skip cleanly.
        continue
    fi
    # Capture combined output; we look for any collection-error signal
    # OR a non-zero exit.
    output="$(cd "$proj_dir" && uv run pytest --collect-only -q 2>&1)"
    exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        failed_projects+=("$proj (exit=$exit_code)")
        continue
    fi
    if echo "$output" | grep -E "(ERROR|FAILED|ImportError|ModuleNotFoundError)" >/dev/null; then
        failed_projects+=("$proj (collection errors)")
    fi
done

if [ ${#failed_projects[@]} -gt 0 ]; then
    echo "rule:python-tests-resolve :: fail (${failed_projects[*]})" >&2
    exit 1
fi

echo "rule:python-tests-resolve :: pass (test collection succeeded for all projects)"
exit 0
