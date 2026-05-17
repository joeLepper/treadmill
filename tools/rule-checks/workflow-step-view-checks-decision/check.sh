#!/usr/bin/env bash
# Deterministic check for rule:workflow-step-view-checks-decision.
#
# Reads a list of changed files (one per line) from stdin. For any Python or
# SQL file that references both "workflow_run_steps" and ".status", checks
# that the same file also references output->>'decision' (or carries an
# explicit exemption comment).
#
# A file may carry an exemption by including this comment anywhere:
#   # rule:workflow-step-view-checks-decision: exempt
#
# Exits 0 (pass) if all relevant files satisfy the check or if no relevant
# files changed.
# Exits 1 (fail) if any relevant file reads status without decision.
#
# Usage:
#   git diff --name-only main... | tools/rule-checks/workflow-step-view-checks-decision/check.sh

set -euo pipefail

EXEMPTION_MARKER="rule:workflow-step-view-checks-decision: exempt"

fail=0

while IFS= read -r path; do
    [ -z "$path" ] && continue
    [[ "$path" =~ \.(py|sql)$ ]] || continue
    [ -f "$path" ] || continue

    # Only check files that reference workflow_run_steps
    grep -q "workflow_run_steps" "$path" || continue

    # Only check files that also read .status
    grep -qE "\.status\b" "$path" || continue

    # Skip files carrying an explicit exemption
    if grep -q "$EXEMPTION_MARKER" "$path"; then
        echo "rule:workflow-step-view-checks-decision :: exempt :: $path"
        continue
    fi

    # Require that the file also reads decision from the output JSON
    if ! grep -qE "(output.*'decision'|output.*\"decision\"|->>'decision'|->\"decision\"|decision.*output)" "$path"; then
        echo "rule:workflow-step-view-checks-decision :: fail :: $path reads workflow_run_steps.status without output->>'decision'" >&2
        fail=1
    fi
done

if [ "$fail" -eq 0 ]; then
    echo "rule:workflow-step-view-checks-decision :: pass"
    exit 0
fi

exit 1
