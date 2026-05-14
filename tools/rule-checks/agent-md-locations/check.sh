#!/usr/bin/env bash
# rule:agent-md-locations check — for each location listed in the rule's
# payload.locations, verify <location>/AGENT.md exists in the repo. Exits
# 0 on full coverage; non-zero with a list of missing files otherwise.
set -euo pipefail

# Resolve repo root (script lives at tools/rule-checks/agent-md-locations/check.sh)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RULE_YAML="$REPO_ROOT/docs/knowledge-base/rules/agent-md-locations.yaml"

missing=()
while IFS= read -r loc; do
  [ -z "$loc" ] && continue
  if [ ! -f "$REPO_ROOT/$loc/AGENT.md" ]; then
    missing+=("$loc/AGENT.md")
  fi
done < <(
  python3 -c "
import yaml
d = yaml.safe_load(open('$RULE_YAML'))
for p in d.get('payload', {}).get('locations', []):
    print(p)
"
)

if [ ${#missing[@]} -gt 0 ]; then
  echo "agent-md-locations: missing AGENT.md at configured locations:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
fi
exit 0
