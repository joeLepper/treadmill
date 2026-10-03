---
date: 2026-10-03
trigger: surprise
status: captured
related: ADR-0008
---

# Learning: A destructive (break-fire) review needs an isolated checkout, never the author's live worktree

## Trigger
During the slice-8 PR (#132) review, a reviewer sibling (Donna) ran foil "break-fires"
(edit a source file to break an invariant → confirm the test reds → revert) inside the
AUTHOR's live git worktree, because that session's sandbox could not
`git worktree add` a separate checkout. The reviewer used cp-backup→break→restore and
restored the file to the committed SHA (`c7773dd`). That restore CLOBBERED the author's
UNCOMMITTED in-flight edits (a quarantine fix the panel had just required). The reviewer
flagged it honestly; the author recovered by re-applying the three wiped edits from
context.

## Observation
A cp-backup→break→restore cycle assumes the baseline equals the file on disk. That is
false when the author has uncommitted changes: the "restore" writes the committed SHA over
the author's newer, unsaved work and destroys it. A shared live worktree is not a safe
place for destructive review.

## Generalization
Two sessions operating on one working tree is unsafe not only for branch/HEAD moves (the
known worktree rule) but for FILE CONTENT during destructive review. We tend to borrow the
author's worktree when a sandbox blocks `git worktree add`, and we tend to leave work
uncommitted while iterating — the combination loses work.

## Proposed rule
A reviewer running destructive break-fires works on an ISOLATED checkout at the committed
SHA (a dedicated worktree or a fresh clone), never in another session's live worktree. If
the sandbox cannot create one, the reviewer reviews read-only and runs foils against a
fresh clone at the SHA — it does not cp-break-restore in a tree it does not own. Reciprocally,
the author commits work-in-progress (even a WIP commit) before a reviewer touches the tree,
so any clobber is recoverable with `git restore` from HEAD.

## Proposed remediation
- Break-fire protocol: before any cp-break-restore, assert `git status` is clean in that
  tree and abort if dirty; prefer a dedicated worktree/clone at the SHA.
- A reviewer whose sandbox blocks `git worktree add` gets a fresh clone path instead of
  borrowing the author's tree.
- Authors commit WIP before handing a shared tree to a reviewer.

## Notes
Reinforces [[feedback_parallel_siblings_use_dedicated_worktrees]] and
[[feedback_shared_checkout_head_can_move_mid_task]] — this is the FILE-CONTENT variant
(destructive review), not just the HEAD-move variant. No lasting harm this time; the edits
were recovered from the session's context.
