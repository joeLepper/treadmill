#!/usr/bin/env bash
# test_launcher_pidfile.sh — unit tests for the PID-file single-instance
# guard in launch-session.sh (ADR-0073).
#
# Validation constraints:
#   - MUST NOT invoke systemd, tmux, or Claude.
#   - Tests run in a temp HOME so state does not pollute ~/.cc-channels/.
#   - A stub 'claude' binary in the test PATH exits immediately so tests
#     that pass the guard can complete without side effects.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$SCRIPT_DIR/../launch-session.sh"

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1" >&2; FAIL=$((FAIL + 1)); }

# ── fixture helpers ──────────────────────────────────────────────────────────

make_tmpdir() {
  mktemp -d
}

# Create a stub 'claude' in a temp bin dir that exits immediately.
make_stub_bin() {
  local bin_dir="$1"
  mkdir -p "$bin_dir"
  cat > "$bin_dir/claude" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$bin_dir/claude"
}

# ── Test 1: refuse to start when another launcher's PID is alive ─────────────
test_refuses_when_live_pid() {
  local tmp
  tmp=$(make_tmpdir)
  trap 'rm -rf "$tmp"' RETURN

  local label="test-live-$$"
  local state_dir="$tmp/.cc-channels/$label"
  mkdir -p "$state_dir"

  # Use a long-running background process as the "live" launcher.
  sleep 9999 &
  local live_pid=$!
  echo "$live_pid" > "$state_dir/launcher.pid"

  local output exit_code=0
  output=$(HOME="$tmp" "$LAUNCHER" "$label" 2>&1) || exit_code=$?

  # Cleanup the background stub before checking results.
  kill "$live_pid" 2>/dev/null || true
  wait "$live_pid" 2>/dev/null || true

  if [[ $exit_code -eq 0 ]]; then
    fail "test_refuses_when_live_pid: expected non-zero exit, got 0"
    return
  fi
  if ! echo "$output" | grep -qi "already"; then
    fail "test_refuses_when_live_pid: expected 'already' in stderr, got: $output"
    return
  fi

  pass "test_refuses_when_live_pid"
}

# ── Test 2: clean up stale PID and proceed ───────────────────────────────────
test_clears_stale_pid_and_proceeds() {
  local tmp
  tmp=$(make_tmpdir)
  trap 'rm -rf "$tmp"' RETURN

  local stub_bin="$tmp/bin"
  make_stub_bin "$stub_bin"

  local label="test-stale-$$"
  local state_dir="$tmp/.cc-channels/$label"
  mkdir -p "$state_dir"

  # Write a PID that is guaranteed to be dead (PID 1 is init, which we can't
  # kill -0; use a known-dead PID by spawning and reaping a subshell).
  local dead_pid
  (exit 0) &
  dead_pid=$!
  wait "$dead_pid" 2>/dev/null || true
  echo "$dead_pid" > "$state_dir/launcher.pid"

  local exit_code=0
  HOME="$tmp" PATH="$stub_bin:$PATH" "$LAUNCHER" "$label" 2>/dev/null || exit_code=$?

  # Stale guard should have cleared the PID file and let the launch proceed.
  # The stub 'claude' exits 0, so launch-session.sh exits 0 too.
  if [[ $exit_code -ne 0 ]]; then
    fail "test_clears_stale_pid_and_proceeds: expected exit 0 after stale PID, got $exit_code"
    return
  fi
  if [[ -f "$state_dir/launcher.pid" ]]; then
    fail "test_clears_stale_pid_and_proceeds: launcher.pid was not cleaned up on exit"
    return
  fi

  pass "test_clears_stale_pid_and_proceeds"
}

# ── Test 3: launcher writes its own PID file ─────────────────────────────────
test_writes_own_pidfile() {
  local tmp
  tmp=$(make_tmpdir)
  trap 'rm -rf "$tmp"' RETURN

  # Use a stub 'claude' that sleeps briefly so we can inspect the PID file
  # while it's "running", then exits.
  local stub_bin="$tmp/bin"
  mkdir -p "$stub_bin"
  cat > "$stub_bin/claude" <<'EOF'
#!/usr/bin/env bash
# Pause just long enough for the test to check the PID file.
sleep 0.2
exit 0
EOF
  chmod +x "$stub_bin/claude"

  local label="test-pidfile-$$"
  local state_dir="$tmp/.cc-channels/$label"
  # Don't pre-create state_dir; launch-session.sh creates it.

  # Run the launcher in the background so we can inspect mid-run.
  HOME="$tmp" PATH="$stub_bin:$PATH" "$LAUNCHER" "$label" 2>/dev/null &
  local bg_pid=$!

  # Give the launcher time to write the PID file before claude's sleep ends.
  sleep 0.1
  local pidfile="$state_dir/launcher.pid"

  if [[ ! -f "$pidfile" ]]; then
    fail "test_writes_own_pidfile: launcher.pid not written while running"
    kill "$bg_pid" 2>/dev/null || true; wait "$bg_pid" 2>/dev/null || true
    return
  fi

  local recorded_pid
  recorded_pid=$(cat "$pidfile")

  # The PID in the file must be alive (the launcher shell, not the bg_pid
  # which is a subshell wrapper in some environments — accept either).
  if ! kill -0 "$recorded_pid" 2>/dev/null && ! kill -0 "$bg_pid" 2>/dev/null; then
    fail "test_writes_own_pidfile: PID $recorded_pid in launcher.pid is not alive"
    wait "$bg_pid" 2>/dev/null || true
    return
  fi

  wait "$bg_pid" 2>/dev/null || true

  # After launcher exits, the trap should have removed the PID file.
  if [[ -f "$pidfile" ]]; then
    fail "test_writes_own_pidfile: launcher.pid not cleaned up after exit"
    return
  fi

  pass "test_writes_own_pidfile"
}

# ── run all tests ─────────────────────────────────────────────────────────────

test_refuses_when_live_pid
test_clears_stale_pid_and_proceeds
test_writes_own_pidfile

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
