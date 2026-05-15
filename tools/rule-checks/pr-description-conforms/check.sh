#!/usr/bin/env bash
# Deterministic check for rule:pr-description-conforms.
#
# Reads the PR body from stdin or fetches it via `gh pr view`. Verifies that
# all five required section headers are present:
#   - ## Summary
#   - ## Why
#   - ## Test plan
#   - ## Validation
#   - ## Refs
#
# Exits 0 if all required sections are present.
#
# Exits 1 (fail) if any required section is missing.
#
# Usage:
#   cat pr-body.txt | tools/rule-checks/pr-description-conforms/check.sh
#   # OR
#   tools/rule-checks/pr-description-conforms/check.sh  # fetches current PR via gh pr view

set -euo pipefail

declare -a required_sections=(
    "## Summary"
    "## Why"
    "## Test plan"
    "## Validation"
    "## Refs"
)

# Resolution order for PR body:
#   1. PR_NUMBER env var (set by the validation runtime in a worker
#      container — most reliable when ``gh pr view`` without args can't
#      auto-detect the PR from the working tree).
#   2. Piped stdin (operator/local-test invocation).
#   3. ``gh pr view`` with no args (interactive operator use only).
if [ -n "${PR_NUMBER:-}" ]; then
    pr_body=$(gh pr view "$PR_NUMBER" --json body --jq '.body' 2>/dev/null || echo "")
elif [ -t 0 ]; then
    pr_body=$(gh pr view --json body --jq '.body' 2>/dev/null || echo "")
else
    pr_body=$(cat)
fi

if [ -z "$pr_body" ]; then
    echo "rule:pr-description-conforms :: fail (could not read PR body)" >&2
    exit 1
fi

missing_sections=()
for section in "${required_sections[@]}"; do
    if ! echo "$pr_body" | grep -q "^${section}$"; then
        missing_sections+=("$section")
    fi
done

if [ ${#missing_sections[@]} -gt 0 ]; then
    echo "rule:pr-description-conforms :: fail (missing sections: ${missing_sections[*]})" >&2
    exit 1
fi

echo "rule:pr-description-conforms :: pass (all required sections present)"
exit 0
