---
date: 2026-06-08
trigger: pattern
status: captured
related: ADR-0084, plan-2026-06-08-adr-0084-coordinator-implementation
---

# Learning: Pre-existing test failures surface in worktree PRs without attribution

## Trigger

Bert's Task 2B PR (#254) reported 1 failing test (`test_starters` — `ModuleNotFoundError: treadmill_cli workspace install gap`). The failure reproduces on clean main HEAD with Task 2B changes stashed, confirming it is pre-existing and not introduced by the PR.

## Observation

When siblings work in per-label git worktrees against main, any pre-existing test failure in the suite appears in their CI run and generates noise that looks like a regression. The author must explicitly verify "reproduces on stashed main" before it becomes a blocker conversation.

## Generalization

Pre-existing failures are invisible in the day-to-day workflow until a new PR touches the test suite or runs CI. Per-worktree isolation surfaces them clearly (one author per run), which is a feature — but coordinators need a fast triage path: does the failure reproduce on HEAD without these changes? If yes, it is a known skip, not the author's problem.

## Proposed rule

When a PR's CI reports a test failure, check whether the failure reproduces on clean main HEAD (changes stashed) before asking the author to fix it. If it does reproduce, mark it as a known pre-existing issue and file a separate follow-up; do not block the PR.

## Proposed remediation

Add a `known_failing_tests` list to a top-level CI config or a `pytest.ini` `xfail` marker so pre-existing failures show as XFAIL rather than ERROR. Alternatively, open a tracked issue per pre-existing failure so coordinators can reference it by number rather than re-diagnosing each time.

## Notes

- `test_starters` failure: `ModuleNotFoundError` on `treadmill_cli` — root cause is a missing workspace install step in the agent Dockerfile, not a test-code bug.
- Resolution in this instance: coordinator ruled leave-as-known-skip, track as follow-up, do not fold fix into Task 2B scope.
