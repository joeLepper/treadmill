# 2026-08-13 — No-auto-adoption static falsifier (ADR-0095, task 4faf7c6c)

PR: TBD (opened by worker-joelepper-treadmill-1, task 4faf7c6c)

## What changed

- Added `services/api/tests/test_no_auto_host_adoption.py` — a sandbox-safe
  static falsifier (no Postgres, no network, no Docker).

  The file contains:
  - `scan_tree(root, this_file)` — module-level scan function shared by both
    tests. Walks `*.py` files (excluding docs/, tests/, .git/, the test itself)
    and `tools/cc-channels/` shell launchers. Checks two things:
    1. No banned identifier (`adopt_on_unreachable`, `reassign_on_unreachable`,
       `relocate_on_unreachable`, `auto_migrate`, `steal_labels`,
       `reconcile_hosts`, `rebalance_labels`) appears in any source file.
    2. No Python function contains both a host-unreachable trigger
       (`unreachable`, `host_down`, `heartbeat_timeout`) and a
       relocation/adoption action (`rebind`, `relocate`, `reassign`, `adopt`)
       in the same body.
  - `test_no_auto_adopt_on_host_unreachable` — the standing guard. Calls
    `scan_tree` on the real repo root. Fails with file+line for any violation.
  - `test_falsifier_can_fail` — meta-test. Creates a temp file containing
    `adopt_on_unreachable`, runs `scan_tree` on the temp tree, and asserts
    a violation is reported. Prevents the "green-but-asserts-nothing" failure.

## ADR-0095 Risks "Check:" line

ADR-0095 (`docs/adrs/0095-named-agents-bind-to-hosts.md`) is not yet on main
(the 0093–0098 ADRs are proposed drafts). The Risks section "Check: none yet —
see Follow-ups" update — pointing at `services/api/tests/test_no_auto_host_adoption.py`
— is left for the ADR-landing PR to apply, per the task brief.

## Why

The 2026-08-11 outage was caused by exec_otp's automatic agent-adoption on
host-unreachable. ADR-0095 Decision line 25 prohibits any automatic relocation:
an agent moves hosts only by explicit operator or coordinator decision. This
test is the committed Check for that invariant — the guard that Treadmill never
grows an automatic adoption path.
