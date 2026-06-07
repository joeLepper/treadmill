#!/usr/bin/env bash
# Deterministic check for rule:alembic-migration-runnable (ADR-0080).
#
# Catches two failure modes seen in production on 2026-06-05 against
# the ADR-0076 PR A worker iteration:
#
#   1. TypeError-shaped misuse of alembic op.* (e.g. wrong arg order on
#      ``op.create_check_constraint``). The bug crashes at the upgrade
#      function body, before the DB sees the DDL — invisible to
#      schema-shape tests but caught by ``alembic upgrade --sql head``.
#
#   2. Multi-head collisions (two migrations chaining off the same
#      parent). Caught by ``alembic heads --resolve-dependencies``
#      returning more than one (head) line.
#
# Exits 0 when no migration file is in the changed-files list (no-op),
# or both gates pass. Exits 1 with a clear diagnostic on either gate
# failure.
#
# Invocation contracts (in priority order):
#   1. ``$1`` is a path to a newline-delimited changed-files list
#      (test-harness contract). Used for the no-op detector.
#   2. ``CHANGED_FILES`` env var holds the same shape (validation
#      runtime contract). Same purpose.
#   3. Neither set: run both gates unconditionally (CI / manual run).
#
# Re: the ``alembic upgrade --sql head`` step — services/api's alembic
# env.py returns a placeholder URL in offline mode when DATABASE_URL is
# absent (ADR-0080), so the gate runs in the worker sandbox without a
# live DB.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ALEMBIC_DIR="$REPO_ROOT/services/api/alembic/versions"

# ── No-op short-circuit ──────────────────────────────────────────────
# If a changed-files list was supplied AND none of the entries point
# into the alembic versions directory, the check has nothing to do.
# Fall through (i.e. run the gates) when the list isn't supplied —
# the validation runtime may not always pass it and we'd rather over-
# run than silently skip.
CHANGED_FILES_PATH="${1:-${CHANGED_FILES:-}}"
if [ -n "$CHANGED_FILES_PATH" ] && [ -f "$CHANGED_FILES_PATH" ]; then
  if ! grep -qE '^services/api/alembic/versions/' "$CHANGED_FILES_PATH"; then
    # No alembic touches; cleanly pass.
    exit 0
  fi
fi

# ── Sanity: directory must exist ─────────────────────────────────────
if [ ! -d "$ALEMBIC_DIR" ]; then
  echo "rule:alembic-migration-runnable :: fail (alembic versions dir missing: $ALEMBIC_DIR)" >&2
  exit 1
fi

cd "$REPO_ROOT/services/api"

# ── Gate 1: exactly one alembic head ─────────────────────────────────
# `alembic heads --resolve-dependencies` outputs one line per head.
# A pure-tree set has exactly one (head)-tagged entry; multi-head
# means two migrations chained off the same parent.
HEADS_OUTPUT="$(uv run alembic heads --resolve-dependencies 2>&1)"
HEADS_EXIT=$?
if [ "$HEADS_EXIT" -ne 0 ]; then
  echo "rule:alembic-migration-runnable :: fail (alembic heads exited $HEADS_EXIT)" >&2
  printf '%s\n' "$HEADS_OUTPUT" >&2
  exit 1
fi
HEAD_COUNT="$(printf '%s\n' "$HEADS_OUTPUT" | grep -cE '\(head\)' || true)"
if [ "$HEAD_COUNT" -ne 1 ]; then
  echo "rule:alembic-migration-runnable :: fail (expected 1 alembic head, found $HEAD_COUNT — multi-head collision)" >&2
  printf '%s\n' "$HEADS_OUTPUT" >&2
  echo "remediation: re-chain the offending migration's down_revision off the latest head, or add a merge revision." >&2
  exit 1
fi

# ── Gate 2: alembic upgrade --sql head produces DDL ──────────────────
# Catches TypeError-shaped misuse of op.* in the upgrade() function
# body — the function loads but raises during the DDL generation pass,
# producing a non-zero exit OR an empty DDL set.
SQL_OUTPUT="$(uv run alembic upgrade --sql head 2>&1)"
SQL_EXIT=$?
if [ "$SQL_EXIT" -ne 0 ]; then
  echo "rule:alembic-migration-runnable :: fail (alembic upgrade --sql head exited $SQL_EXIT)" >&2
  printf '%s\n' "$SQL_OUTPUT" >&2
  echo "remediation: read the traceback — most often this is an op.* argument shape bug (wrong positional vs. keyword order)." >&2
  exit 1
fi
# Use a here-string instead of pipefail-vulnerable `printf | grep -q` —
# grep -q closes stdin on first match, SIGPIPE's printf, pipefail
# propagates the non-zero, and `! pipeline` inverts the success-on-match
# into a false-positive failure path. Here-string sidesteps it.
if ! grep -qE 'CREATE|ALTER|INSERT|DROP' <<<"$SQL_OUTPUT"; then
  echo "rule:alembic-migration-runnable :: fail (alembic upgrade --sql head produced no DDL)" >&2
  echo "either every migration is a no-op (suspicious) or the generator failed silently." >&2
  exit 1
fi

exit 0
