#!/usr/bin/env bash
# review-pr.sh — build a review artifact from a PR / branch / file and run the
# cross-model review panel over it (ADR-0107). Harness-agnostic: pure shell, so
# Claude Code and Codex agents call it identically.
#
# Usage:
#   review-pr.sh <PR-number | branch | file> [options]
# Options:
#   --base <ref>            merge-base ref for the diff (default: origin/main)
#   --author-family <fam>   requester's model family, excluded from coverage
#   --json                  machine-readable panel output
#   --exclude <glob>        extra git pathspec exclude (repeatable)
#   --timeout <sec>         per-leg timeout passed to the panel
#
# Exit code is the panel's own: 0 ONLY on a non-block verdict with a cross-family
# quorum; block / degraded / one-model-pass all exit non-zero (fail closed).
set -euo pipefail

PANEL="${REVIEW_PANEL_PY:-$HOME/treadmill/tools/model-review-panel/panel.py}"
BASE="origin/main"
FORMAT="human"
AUTHOR_FAMILY=""
TIMEOUT=""
TARGET=""
# Generated / vendored paths that only pad a review. Conservative defaults; add
# more with --exclude. Kept as pathspec exclusions for the git-diff paths.
EXCLUDES=(
  ':!*/e2e/conformance/baselines/*'
  ':!*/e2e/conformance/snapshot/*'
  ':!*.baseline.json'
  ':!**/package-lock.json'
  ':!**/pnpm-lock.yaml'
  ':!**/yarn.lock'
  ':!**/*.min.js'
  ':!**/*.min.css'
  ':!**/dist/*'
  ':!**/build/*'
  ':!**/__snapshots__/*'
)

err() { echo "review-pr: $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)          BASE="$2"; shift 2 ;;
    --author-family) AUTHOR_FAMILY="$2"; shift 2 ;;
    --json)          FORMAT="json"; shift ;;
    --timeout)       TIMEOUT="$2"; shift 2 ;;
    --exclude)       EXCLUDES+=( ":!$2" ); shift 2 ;;
    -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
    -*)              err "unknown option: $1"; exit 2 ;;
    *)               if [[ -n "$TARGET" ]]; then err "one target only"; exit 2; fi; TARGET="$1"; shift ;;
  esac
done

[[ -n "$TARGET" ]] || { err "need a PR number, branch, or file"; exit 2; }
[[ -f "$PANEL" ]] || { err "panel not found at $PANEL (set REVIEW_PANEL_PY)"; exit 2; }

ART="$(mktemp -t review-artifact.XXXXXX.diff)"
trap 'rm -f "$ART"' EXIT

if [[ -f "$TARGET" ]]; then
  # A file (an existing diff, ADR, plan, design note): review it as-is.
  cp "$TARGET" "$ART"
  err "artifact: file $TARGET"
elif [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  # A PR number: resolve its head branch via gh, fetch it, diff with excludes.
  command -v gh >/dev/null || { err "gh not found; needed to resolve PR #$TARGET"; exit 2; }
  HEAD_REF="$(gh pr view "$TARGET" --json headRefName -q .headRefName 2>/dev/null || true)"
  if [[ -n "$HEAD_REF" ]]; then
    git fetch -q origin "$HEAD_REF" 2>/dev/null || true
    git diff "$BASE"...origin/"$HEAD_REF" -- "${EXCLUDES[@]}" > "$ART" 2>/dev/null \
      || git diff "$BASE"...FETCH_HEAD -- "${EXCLUDES[@]}" > "$ART"
    err "artifact: PR #$TARGET ($HEAD_REF) vs $BASE, generated files excluded"
  else
    # Fall back to gh's own diff (no pathspec exclusion available there).
    gh pr diff "$TARGET" > "$ART"
    err "artifact: PR #$TARGET via 'gh pr diff' (no exclude filtering)"
  fi
else
  # A branch name (local or remote): diff vs base with excludes.
  REF="$TARGET"
  git rev-parse --verify -q "$REF" >/dev/null || REF="origin/$TARGET"
  git rev-parse --verify -q "$REF" >/dev/null || { err "no such branch: $TARGET"; exit 2; }
  git diff "$BASE"..."$REF" -- "${EXCLUDES[@]}" > "$ART"
  err "artifact: branch $REF vs $BASE, generated files excluded"
fi

if [[ ! -s "$ART" ]]; then
  err "artifact is empty — nothing to review (is $TARGET already merged into $BASE?)"
  exit 2
fi
err "artifact size: $(wc -l < "$ART") lines"

CMD=( python3 "$PANEL" review --artifact "$ART" --format "$FORMAT" )
[[ -n "$AUTHOR_FAMILY" ]] && CMD+=( --author-family "$AUTHOR_FAMILY" )
[[ -n "$TIMEOUT" ]] && CMD+=( --timeout "$TIMEOUT" )

exec "${CMD[@]}"
