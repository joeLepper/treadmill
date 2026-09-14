---
date: 2026-09-13
trigger: surprise
status: captured
related: ADR-0109
---

# Learning: A new subcommand changes a Typer app's arity and breaks sibling tests

## Trigger
Step 1 of the team-lifecycle plan added a second command (`team down`) to the
`team` Typer app, which already had `team up`. The change merged with a sibling
co-sign (both reviewers verified the new `team down` tests and the changed code).
On push, main went red on the `cli` check: 18 tests in `test_cli_team_up.py`
(a file the diff did NOT touch) began returning exit code 2.

## Observation
Typer/Click auto-invokes the sole command when an app has exactly ONE command,
so `runner.invoke(team_app, ["x/y"])` ran `team up` implicitly. Adding a second
command ended that auto-invoke: the subcommand name became required, so every
bare-repo invocation parsed as an unknown command and exited 2. The regression
lived entirely in files the change did not modify; both reviewers ran only the
new tests and the changed code, not the pre-existing package suite.

## Generalization
Adding or removing a subcommand changes the whole app's parse contract, not just
the new command. The blast radius is every existing test and every caller that
relied on the previous arity — precisely the code a diff-scoped review does not
open.

## Proposed rule
When a change adds or removes a Typer/Click subcommand (or otherwise changes an
app's command arity), run the ENTIRE package test suite before merge, not only
the new tests. A co-sign on such a change asserts the full suite is green.

## Proposed remediation
CI already runs the full `cli` suite; the gap was local pre-merge verification.
Remediation: the reviewer's checklist for a CLI-command change includes
`uv run pytest` for the whole package, not a single new test file.

## Notes
Fix was test-only (prefix `"up"` in the 18 invocations; real usage is already
`treadmill team up <repo>`). Un-redded by daf54f2. Same shape as the standing
"scope in the existing tests of any code you modify" rule, one level up: here the
change modified no shared function, only the app's arity, yet still broke the
siblings.
