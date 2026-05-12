#!/usr/bin/env bash
# Deterministic check for rule:features-ship-with-tests.
#
# Reads a list of changed files (one per line) from stdin. Exits 0 if either:
#   - no production-code file changed (the rule does not apply), or
#   - at least one test file was added or updated alongside the production code.
# Exits 1 (fail) if production code changed but no test file changed.
#
# This is a v0 sketch. The Treadmill rule engine (deferred ADR) will invoke
# it with the canonical "files changed in this PR" list and dispatch the
# remediation per the rule's remediations[] entry on failure.
#
# Usage:
#   git diff --name-only main... | tools/rule-checks/features-ship-with-tests/test-files-changed.sh

set -euo pipefail

production_pattern='^(infra|tools|workers|services)/'
test_pattern='(^|/)tests?/|/test_[^/]+\.py$|_test\.py$'

production_changed=0
tests_changed=0

while IFS= read -r path; do
    [ -z "$path" ] && continue
    if [[ "$path" =~ $test_pattern ]]; then
        tests_changed=1
        continue
    fi
    if [[ "$path" =~ $production_pattern ]]; then
        production_changed=1
    fi
done

if [ "$production_changed" -eq 0 ]; then
    echo "rule:features-ship-with-tests :: pass (no production code changed)"
    exit 0
fi

if [ "$tests_changed" -eq 1 ]; then
    echo "rule:features-ship-with-tests :: pass (tests changed alongside production code)"
    exit 0
fi

echo "rule:features-ship-with-tests :: fail (production code changed without any test files added or updated)" >&2
exit 1
