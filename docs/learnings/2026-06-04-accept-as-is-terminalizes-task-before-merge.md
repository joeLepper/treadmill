---
date: 2026-06-04
trigger: surprise
status: captured
related: ADR-0031, ADR-0048, plan-2026-06-02-claude-usage-limit-fallback
---

# Learning: accept-as-is terminalizes the task before its PR merges

## Trigger

Task `57598e7a` (ADR-0066 schema change): the author opened PR #133 (checks
green) and the run ended `responded-without-change`; the architect triaged it
`accept-as-is`. The task's derived status flipped to `done` while the PR was
still OPEN with zero reviews. Twelve minutes later: no wf-review run had
dispatched, no auto-merge deadline key existed in Redis, and downstream task
`4300e935` sat blocked on `task.57598e7a.pr_merged` — a dependency that could
never fire. Resolved by manually squash-merging #133 (via the REST endpoint;
GraphQL was rate-limited), after which the pr_merged webhook unblocked the
chain normally.

## Observation

`done` is terminal, and terminal status gates further workflow dispatch. An
architect `accept-as-is` verdict reached *before* the PR merged therefore
stranded the PR: nothing remained alive to review it, arm the ADR-0031
cooling-off, or merge it. The system's own approval became the reason the
approval could not be acted on.

## Generalization

Status projections that treat a judgment ("this work is acceptable") as a
lifecycle endpoint ("nothing further will happen") deadlock whenever real-world
steps remain after the judgment. Sibling of the dual-encoding-projections
learning: `done`-as-judgment and `done`-as-merged are adjacent encodings of
one concept, and every consumer that reads only one mis-acts at the boundary.

## Proposed rule

An architect `accept-as-is` on a task whose PR is open must route the task to
`awaiting_review` / merge-eligible — never directly to a terminal status; only
`pr_merged` (or cancel/supersede) terminalizes a task that owns an open PR.

## Proposed remediation

Deterministic check: the stuck-task sweep should flag any task in a terminal
status that owns an OPEN PR (cheap join of task status × PR state) and emit an
escalation (ADR-0062 incident). Auto-remediation candidate: re-arm the
auto-merge eligibility path for green, accept-as-is PRs instead of requiring
an operator merge.

## Notes

Manual-merge precedent: the no-manual-merge rule guards *mid-pipeline* tasks;
a terminal task with a stranded-but-approved PR is the case where operator
merge is correct. GraphQL rate exhaustion (watcher-heavy night) is unrelated
but made the fix path REST-only — `gh api -X PUT .../pulls/N/merge`.
