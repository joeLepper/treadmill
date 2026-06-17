#!/usr/bin/env bash
# Install the repo's git pre-commit hook (secret-leak scanner).
#
# Wires tools/dev-hooks/pre_commit_secret_leak.py as the git pre-commit
# hook. The scanner loads its denylist from the OUT-OF-SOURCE-CONTROL
# ~/.treadmill/codenames.json (override TREADMILL_CODENAMES_FILE) — so it
# only gates on machines that have that operator-local file; a public
# clone without it is unaffected.
#
# Idempotent. If a pre-commit hook already exists and isn't ours, we
# refuse rather than clobber (chain it yourself or remove it first).
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
hook_path="$repo_root/.git/hooks/pre-commit"
scanner="tools/dev-hooks/pre_commit_secret_leak.py"
marker="# treadmill-secret-leak-hook"

if [[ -e "$hook_path" ]] && ! grep -q "$marker" "$hook_path" 2>/dev/null; then
  echo "refusing to overwrite an existing non-treadmill pre-commit hook at $hook_path" >&2
  echo "chain '$marker' into it manually, or remove it and re-run." >&2
  exit 1
fi

cat > "$hook_path" <<EOF
#!/usr/bin/env bash
$marker
exec python3 "\$(git rev-parse --show-toplevel)/$scanner"
EOF
chmod +x "$hook_path"
echo "installed secret-leak pre-commit hook → $hook_path"
echo "denylist source: \${TREADMILL_CODENAMES_FILE:-~/.treadmill/codenames.json} (out of source control)"
