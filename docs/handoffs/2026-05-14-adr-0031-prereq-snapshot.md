# ADR-0031 Prerequisite Snapshot — 2026-05-14

Phase 4 gate for the hands-free-driving trim-2 plan. Confirms that all five
prerequisites named in ADR-0031 §Follow-ups landed before the auto-merge
trigger dispatches.

---

## Prereq 1 — Task #120: wf-feedback duplicate PRs (PR #53)

**Commit:** `90cb467`  
**PR:** #53 — _code disposition skips `gh pr create` on re-author workflows_

Re-author dispositions were pushing to merged-then-deleted branches and
`gh pr create` was opening noise PRs. This commit gates `gh pr create` behind
a check that the branch is not a re-author re-push, making the code disposition
idempotent on repeat runs. Without this fix, auto-merge would silently land
duplicate PRs.

---

## Prereq 2 — Task #121: author-side validation (PR #54)

**Commit:** `f360b3e`  
**PR:** #54 — _author-side validation in code disposition_

Three PRs in the 2026-05-14 push (#31, #33, #35) failed their declared
validation scripts on first run. This commit adds a pre-push validation step
inside the code disposition: the agent's validation script runs before
`git push`, and a failing script aborts the push and routes back to
`wf-feedback`. Without this, auto-merge would bake script-failing drift into
main before any human noticed.

---

## Prereq 3 — Task #124: DB ↔ main reconciliation (PR #55)

**Commit:** `9dd908b`  
**PR:** #55 — _task\_prs branch-name fallback for operator-completed PRs_

Operator-completed PRs were leaving the task layer permanently desynchronized:
the DB's `task_prs` table had no record linking the task to the merged PR, so
downstream dispositions had no `pr_number` to act on. This commit adds a
branch-name fallback in `task_prs` to reconcile operator-completed PRs.
Without this, auto-merge would magnify the desync into a stuck downstream
chain.

---

## Prereq 4 — Task #127: GH Actions CI on PRs (PR #56 + operator note)

**Commit:** `d79eb0d`  
**PR:** #56 — _Add GH Actions CI workflow for Treadmill's own test suite_

Treadmill had no CI gate before this PR; the merge button was the only check.
`wf-validate.decision=pass` means "task-intent validation passed" — necessary
but not sufficient. Without a CI check running the existing test suite, a PR
that passes task-intent validation could still introduce a regression.
This commit wires the test suite as a GH Actions check that must pass before
`mergeability` flips to `mergeable`.

**Operator note:** The CI workflow targets `pull_request` events and covers the
`workers/agent` and `services/api` test suites via `uv run pytest`. The
operator confirmed that the workflow is active and passing on the main branch
as of 2026-05-14.

---

## Prereq 5 — ADR-0032 plan completion (tasks #125 + #126)

ADR-0032 introduced `role-documentarian`, `role-architect`, `wf-doc-amend`,
and `wf-architecture-resolve`. The plan re-fired four tasks through the
trim-2 sequence; all four landed:

### 5a — wf-doc-amend disposition (PR #58)

**Commit:** `c841ddd`  
**PR:** #58 — _workers documentation.py handles wf-doc-amend + Class C escalation_

`workers/agent/treadmill_agent/runner_dispositions/documentation.py` exists,
wired in `runner.py` on `output_kind == 'documentation'`. Class C gaps trigger
a learning at `docs/learnings/` and dispatch `wf-architecture-resolve`.

### 5b — wf-architecture-resolve disposition (PR #59)

**Commit:** `3a88ec4`  
**PR:** #59 — _Operator: hand-impl architecture.py disposition (wf-architecture-resolve)_

`workers/agent/treadmill_agent/runner_dispositions/architecture.py` exists,
parses `ArchitectVerdict`, routes all four verdicts (`amend` / `supersede` /
`accept-as-is` / `uncertain`), and enforces the 5-attempt rework cap per
ADR-0032 Q32.e. Operator hand-implemented after the agent disposition.

### 5c — validator-remediation dispatch (PR #61)

**Commit:** `a7cae9c`  
**PR:** #61 — _docs-current-with-pr.fail → wf-doc-amend (fourth dispatch source)_

`coordination/triggers.py` now dispatches `wf-doc-amend` (not `wf-feedback`)
when `wf-validate.decision=fail` and the failing check is
`docs-current-with-pr`. Other rule failures continue to dispatch `wf-feedback`.
Dedup namespace `docs-amend-run=` added; cap at 5 per task.

### 5d — ADR-0030 backfill re-routed through wf-doc-amend (PR #62)

**Commit:** `70f3d54`  
**PR:** #62 — _Re-fire ADR-0030 backfill plan through wf-doc-amend_

`docs/plans/2026-05-14-adr-0030-diagram-backfill.md` re-authored so all 33
backfill tasks use `workflow: wf-doc-amend`. The ADR-0030 backfill now
dispatches cleanly through the new role without operator scaffolding.

---

## Status

All five ADR-0031 prerequisites are merged into `main` as of 2026-05-14.
The auto-merge trigger (phase 4, task `auto-merge-trigger`) may now be
dispatched.
