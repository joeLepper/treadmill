---
date: 2026-05-18
trigger: surprise
status: captured
related: ADR-0031, ADR-0038, ADR-0042, PR #154
---

# Learning: Auto-merge predicate didn't fire on architect-override completion

## Trigger

PR #153 (task `07c71852-a7b3-4a70-b7aa-f8d2b0190745`, Deploy watcher
module) reached this state on 2026-05-18:

- wf-author failed (silent crash)
- wf-feedback retry authored the work, opened the PR
- wf-validate failed (spurious LLM-judge)
- wf-review changes_requested
- **Three** wf-architecture-resolve verdicts, all `accept-as-is`
- `review.override` + `validate.override` events emitted
- `task_mergeability` VIEW: `mergeable`, `validate=pass`, `review=approved`
- **0 auto-merge-fired Redis keys, PR still OPEN**

The system had done everything right — including ADR-0042's
validate.override fix — but the PR sat in operator limbo.

## Observation

`_AUTO_MERGE_TRIGGER_WORKFLOWS` in
`services/api/treadmill_api/coordination/triggers.py` was scoped to
`{wf-validate, wf-review}`. The auto-merge cooling-off predicate fires
only on `step.completed` for these workflows. When the architect
emits the override events via wf-architecture-resolve's
step.completed, that workflow isn't in the trigger set → predicate
doesn't fire → no cooling-off deadline in Redis → no merge.

The override path was thought through (ADR-0042 explicitly designed
the projection layer), but the *firing* end of the auto-merge wasn't
extended to include the architect's own step.completed. The original
ADR-0038 review-override path had the same shape and was deployed
without auto-merge ever firing on it — we just hadn't hit a case
where it mattered until 2026-05-18.

## Generalization

Architectural fixes that introduce a new path to "mergeable" via
event projection need to also extend whatever trigger fires the
auto-merge predicate. Adding columns to a projection VIEW changes
what `mergeable` means, but the predicate only fires on a finite set
of workflow completions — if the path to mergeable runs through a
workflow not in that set, the VIEW will sit at `mergeable` forever
with no merge.

Sibling shape: any future override channels (e.g. a hypothetical
ci-override or conflict-override) will have the same gap unless we
either (a) explicitly add their emitting workflow to the trigger
set, or (b) fire the predicate on `task_mergeability.changed` events
instead of on `step.completed` for specific workflows. (b) is the
load-bearing fix; (a) is the current pragmatic fix.

## Proposed rule

When introducing a projection-level override for an auto-merge gate
(review, validate, ci, conflict), the implementing PR must also:
- Identify the emitting workflow (which step.completed produces the
  override event).
- Add that workflow to `_AUTO_MERGE_TRIGGER_WORKFLOWS` (or
  equivalent trigger gate in `_maybe_fire_auto_merge`).
- Add a test asserting the auto-merge cooling-off deadline is set
  when the override event lands.

A better long-term design (proposed): the auto-merge poll loop should
react to mergeability changes via a `task_mergeability.changed` event
projection rather than step-workflow gating. That structure would
make the predicate self-firing whenever ANY gate signal changes —
removing the class of bug entirely. (Out of scope for the immediate
fix.)

## Proposed remediation

Shipped in PR #154: add `wf-architecture-resolve` to
`_AUTO_MERGE_TRIGGER_WORKFLOWS`. One-line set extension + 17 lines
of context comment.

Verified end-to-end: synthetic re-fire of an architect step.completed
under #154 set the cooling-off deadline; PR #153 merged automatically
30s later. First end-to-end hands-free auto-merge of the 2026-05-18
push.

## Notes

- The "long-term design" of firing the predicate on
  `task_mergeability.changed` events is potentially an ADR — it
  changes the auto-merge architecture from "named-workflow trigger"
  to "projection-state-change trigger." Worth flagging when the
  next override channel is proposed.
- The 2026-05-17 flush-race learning (PR #142) was a near-miss for
  this same class of bug: the override was emitted but the VIEW
  didn't reflect it in time. PR #142 fixed the visibility (flush);
  PR #154 fixed the firing (trigger set).
