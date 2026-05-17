#!/usr/bin/env bash
# Deterministic check for rule:repo-relative-path-in-docs.
#
# Reads a list of changed files (one per line) from stdin. For each markdown
# file, checks whether it contains path references beginning with the
# repository's own name as a directory prefix ("treadmill/docs/…", etc.).
# Such paths are wrong when written from inside the treadmill repo — they
# should be repo-relative ("docs/…").
#
# Exits 0 (pass) if no such paths are found in changed markdown files.
# Exits 1 (fail) if any markdown file contains a prefixed path reference.
#
# Usage:
#   git diff --name-only main... | tools/rule-checks/repo-relative-path-in-docs/check.sh

set -euo pipefail

REPO_NAME="${REPO_NAME:-treadmill}"

# Top-level directories that would appear after the repo-name prefix.
# Extend this list if the repo grows new top-level dirs.
TOP_LEVEL_DIRS="(docs|services|workers|tools|infra|tests)"

fail=0

while IFS= read -r path; do
    [ -z "$path" ] && continue
    [[ "$path" =~ \.md$ ]] || continue
    [ -f "$path" ] || continue

    if grep -qE "(^|[ \`\"\(])${REPO_NAME}/${TOP_LEVEL_DIRS}/" "$path"; then
        echo "rule:repo-relative-path-in-docs :: fail :: $path contains path prefixed with '${REPO_NAME}/'" >&2
        grep -nE "(^|[ \`\"\(])${REPO_NAME}/${TOP_LEVEL_DIRS}/" "$path" | head -5 >&2
        fail=1
    fi
done

if [ "$fail" -eq 0 ]; then
    echo "rule:repo-relative-path-in-docs :: pass"
    exit 0
fi

exit 1
