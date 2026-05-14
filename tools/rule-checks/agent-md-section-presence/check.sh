#!/usr/bin/env bash
# Deterministic check for rule:agent-md-section-presence.
#
# Reads a list of changed files (one per line) from stdin. For each AGENT.md
# file, verifies that all five required section headers are present:
#   - ## Purpose
#   - ## Key surfaces
#   - ## Recent changes
#   - ## Pitfalls
#   - ## Navigation
#
# Exits 0 if all AGENT.md files contain all five sections, or if no AGENT.md
# files were changed (rule does not apply).
#
# Exits 1 (fail) if any AGENT.md file is missing one or more required sections.
#
# Usage:
#   git diff --name-only main... | tools/rule-checks/agent-md-section-presence/check.sh

set -euo pipefail

declare -a required_sections=(
    "## Purpose"
    "## Key surfaces"
    "## Recent changes"
    "## Pitfalls"
    "## Navigation"
)

agent_md_files=()
failed=0

while IFS= read -r path; do
    [ -z "$path" ] && continue
    if [[ "$path" == *"/AGENT.md" ]] || [[ "$path" == "AGENT.md" ]]; then
        agent_md_files+=("$path")
    fi
done

# If no AGENT.md files changed, the rule does not apply.
if [ ${#agent_md_files[@]} -eq 0 ]; then
    echo "rule:agent-md-section-presence :: pass (no AGENT.md files changed)"
    exit 0
fi

# Check each AGENT.md file for required sections.
for file in "${agent_md_files[@]}"; do
    if [ ! -f "$file" ]; then
        echo "rule:agent-md-section-presence :: fail ($file does not exist)" >&2
        failed=1
        continue
    fi

    missing_sections=()
    for section in "${required_sections[@]}"; do
        if ! grep -q "^${section}$" "$file"; then
            missing_sections+=("$section")
        fi
    done

    if [ ${#missing_sections[@]} -gt 0 ]; then
        echo "rule:agent-md-section-presence :: fail ($file missing: ${missing_sections[*]})" >&2
        failed=1
    fi
done

if [ "$failed" -eq 1 ]; then
    exit 1
fi

echo "rule:agent-md-section-presence :: pass (all AGENT.md files have required sections)"
exit 0
