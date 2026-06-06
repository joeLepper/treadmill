#!/usr/bin/env bash
# Tests for rule:alembic-migration-runnable (ADR-0080).
#
# Four scenarios:
#   1. no-op short-circuit (changed-files list contains no alembic paths)
#   2. happy path (current main: migration set is valid + single head)
#   3. failure: TypeError in op.create_check_constraint arg order
#   4. failure: multi-head (two migrations chain off the same parent)
#
# Each test stages its fixture via a side branch in the alembic versions
# directory, runs check.sh, captures exit + stderr, and reverts the
# staging cleanly so the tests don't pollute the working tree. Run
# from the repo root.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CHECK="$SCRIPT_DIR/check.sh"
ALEMBIC_DIR="$REPO_ROOT/services/api/alembic/versions"

failures=0

# ── helpers ─────────────────────────────────────────────────────────

# `assert_exit <expected> <actual> <label>`. Records a failure for the
# summary but does NOT exit — we want every scenario to report.
assert_exit() {
  local expected=$1 actual=$2 label=$3
  if [ "$actual" -ne "$expected" ]; then
    echo "  FAIL: $label expected exit=$expected got $actual" >&2
    failures=$((failures + 1))
  else
    echo "  ok: $label exit=$actual"
  fi
}

# `cleanup_fixture <path>`. Removes a fixture file if it exists.
cleanup_fixture() {
  if [ -f "$1" ]; then
    rm -f "$1"
  fi
}

# Resolve the current head revision — needed for the multi-head test
# to know what parent both forks should chain off of.
current_head() {
  cd "$REPO_ROOT/services/api"
  uv run alembic heads --resolve-dependencies 2>/dev/null \
    | awk '/\(head\)/ {print $1; exit}'
}

# ── test 1: no-op short-circuit ────────────────────────────────────

test_no_op_when_no_alembic_files() {
  echo "test_no_op_when_no_alembic_files"
  local list
  list=$(mktemp)
  cat >"$list" <<EOF
README.md
services/api/treadmill_api/config.py
workers/agent/treadmill_agent/runner.py
EOF
  "$CHECK" "$list"
  local rc=$?
  rm -f "$list"
  assert_exit 0 "$rc" "no-op short-circuit"
}

# ── test 2: happy path ─────────────────────────────────────────────

test_happy_path() {
  echo "test_happy_path"
  # Use a changed-files list that DOES include the alembic dir so the
  # no-op short-circuit is bypassed. The real migration set on main
  # should pass both gates.
  local list
  list=$(mktemp)
  echo "services/api/alembic/versions/20260605_1700_validator_gold_rows.py" > "$list"
  "$CHECK" "$list"
  local rc=$?
  rm -f "$list"
  assert_exit 0 "$rc" "happy path (current main is valid)"
}

# ── test 3: TypeError-shaped misuse of op.* ────────────────────────

test_failure_typeerror_in_upgrade() {
  echo "test_failure_typeerror_in_upgrade"
  local head
  head=$(current_head)
  local fixture="$ALEMBIC_DIR/99999999_9999_test_typeerror_fixture.py"
  cat >"$fixture" <<EOF
"""Fixture for test_failure_typeerror_in_upgrade — intentionally bad arg order.

This reproduces the exact bug shipped on ADR-0076 PR A pass 1:
\`create_check_constraint(name, condition, table_name=...)\` passes the
condition string as the second positional, then tries to bind table_name
as a kwarg — duplicate-arg TypeError at upgrade time.

Revision ID: 99999999_9999
Revises: $head
"""
from __future__ import annotations
from collections.abc import Sequence
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = "99999999_9999"
down_revision: Union[str, Sequence[str], None] = "$head"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_fixture_typeerror",
        "(1 = 1)",
        table_name="repo_configs",
    )


def downgrade() -> None:
    op.drop_constraint("ck_fixture_typeerror", "repo_configs", type_="check")
EOF
  local list
  list=$(mktemp)
  echo "services/api/alembic/versions/99999999_9999_test_typeerror_fixture.py" > "$list"
  "$CHECK" "$list" >/dev/null 2>&1
  local rc=$?
  cleanup_fixture "$fixture"
  rm -f "$list"
  assert_exit 1 "$rc" "TypeError-shaped op.create_check_constraint detected"
}

# ── test 4: multi-head collision ───────────────────────────────────

test_failure_multi_head() {
  echo "test_failure_multi_head"
  local head
  head=$(current_head)
  local fixture_a="$ALEMBIC_DIR/99999999_aaaa_test_fork_a.py"
  local fixture_b="$ALEMBIC_DIR/99999999_bbbb_test_fork_b.py"

  cat >"$fixture_a" <<EOF
"""Fixture A for multi-head test."""
from __future__ import annotations
from collections.abc import Sequence
from typing import Union
from alembic import op

revision: str = "99999999_aaaa"
down_revision: Union[str, Sequence[str], None] = "$head"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
EOF

  cat >"$fixture_b" <<EOF
"""Fixture B for multi-head test (forks off the same parent as A)."""
from __future__ import annotations
from collections.abc import Sequence
from typing import Union
from alembic import op

revision: str = "99999999_bbbb"
down_revision: Union[str, Sequence[str], None] = "$head"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
EOF

  local list
  list=$(mktemp)
  cat >"$list" <<EOF
services/api/alembic/versions/99999999_aaaa_test_fork_a.py
services/api/alembic/versions/99999999_bbbb_test_fork_b.py
EOF

  "$CHECK" "$list" >/dev/null 2>&1
  local rc=$?
  cleanup_fixture "$fixture_a"
  cleanup_fixture "$fixture_b"
  rm -f "$list"
  assert_exit 1 "$rc" "multi-head collision detected"
}

# ── run ─────────────────────────────────────────────────────────────

echo "tools/rule-checks/alembic-migration-runnable/test_check.sh"
echo "---"

test_no_op_when_no_alembic_files
test_happy_path
test_failure_typeerror_in_upgrade
test_failure_multi_head

echo "---"
if [ "$failures" -gt 0 ]; then
  echo "FAILED: $failures of 4 tests failed" >&2
  exit 1
fi
echo "PASSED: 4 of 4 tests"
exit 0
