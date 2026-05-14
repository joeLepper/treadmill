#!/usr/bin/env bash
# Deterministic check for rule:adr-and-plan-has-diagram.
#
# Reads a list of changed files (one per line) from stdin. For each file
# under docs/adrs/ or docs/plans/, verifies it contains a Mermaid diagram
# block (``` ```mermaid ... ``` ```). The closing fence must be on its own
# line at column 0 to avoid false positives where "mermaid" appears only
# in prose.
#
# Exits 0 if:
#   - no ADR or plan files changed (rule does not apply), or
#   - all changed ADR/plan files contain valid Mermaid blocks
# Exits 1 (fail) if any changed ADR/plan file lacks a valid Mermaid block.
#
# Usage:
#   git diff --name-only main... | tools/rule-checks/adr-and-plan-has-diagram/check.sh

set -euo pipefail

# Pattern to match files under docs/adrs/ or docs/plans/
diagram_file_pattern='^docs/(adrs|plans)/'

diagram_files_changed=0
diagram_files_missing_block=0
missing_files=""

while IFS= read -r path; do
    [ -z "$path" ] && continue

    # Skip if not a docs/adrs or docs/plans file
    if ! [[ "$path" =~ $diagram_file_pattern ]]; then
        continue
    fi

    diagram_files_changed=1

    # Check if file exists (in case of deletion)
    if [ ! -f "$path" ]; then
        continue
    fi

    # Check for mermaid block opening: ``` ```mermaid (may have whitespace before/after)
    if ! grep -q '^ *``` *mermaid' "$path"; then
        diagram_files_missing_block=1
        missing_files="$missing_files\n  - $path (missing opening \`\`\`mermaid)"
        continue
    fi

    # Check for closing fence on its own line at column 0: exactly ``` with optional trailing whitespace
    if ! grep -q '^``` *$' "$path"; then
        diagram_files_missing_block=1
        missing_files="$missing_files\n  - $path (missing closing \`\`\` at column 0)"
        continue
    fi
done

if [ "$diagram_files_changed" -eq 0 ]; then
    echo "rule:adr-and-plan-has-diagram :: pass (no ADR or plan files changed)"
    exit 0
fi

if [ "$diagram_files_missing_block" -eq 0 ]; then
    echo "rule:adr-and-plan-has-diagram :: pass (all ADR/plan files contain valid Mermaid blocks)"
    exit 0
fi

printf "rule:adr-and-plan-has-diagram :: fail (ADR/plan files missing Mermaid diagrams):%b\n" "$missing_files" >&2
exit 1
