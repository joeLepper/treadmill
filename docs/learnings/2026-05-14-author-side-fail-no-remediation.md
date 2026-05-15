---
date: 2026-05-14
trigger: surprise
status: crystallized-into-ADR-0037
related: ADR-0029, plan-2026-05-14-hands-free-driving-trim2
---

# Learning: Author-side validation fail leaves the task stuck

## Trigger

During the trim-2 Phase 4 cascade, the `per-plan-opt-out-parser` task (task
`0ac62421-19ce-43a8-966a-7e0a9f540fc9`) ran wf-author, the code-author's
self-validation step (task #121 / ADR ralph-loop) caught a failure
(`grep: .claude/skills/plan/SKILL.md: No such file or directory` —
script-path bug in the plan's validation block), emitted
`step.completed` with `decision=fail`, and stopped. No `wf-feedback`
dispatch fired; no re-author; no PR. Task sat in a dead state until
operator intervention (re-fired via `_create_and_publish_run` after
patching the plan's script).

## Observation

The coordination consumer dispatches `wf-feedback` on
`wf-validate.decision=fail` and `wf-review.verdict=request_changes`,
but **not** on `wf-author.decision=fail`. Author-side validation
(task #121's mechanism) produces a `decision=fail` from the wf-author
step itself, and that case has no downstream trigger.

Confirmation: parser task event log shows `step.completed` at 22:39
followed by **silence** until the operator-triggered re-fire at 23:32.

## Generalization

We added author-side validation to keep authors honest, but didn't
extend the trigger evaluator to treat an honest author-side fail as
a remediation event. The mechanism succeeds in its narrow job
(don't push failing work) but fails operationally (the task stops
moving). For hands-free driving this is a hard block: the failure
mode that #121 is *designed to produce* has no continuation path.

## Proposed rule

Every `decision=fail` from a wf-author step that completed
author-side validation should dispatch `wf-feedback` against the
same task with the validation rationale as the feedback payload —
identical to the wf-validate.fail path.

## Proposed remediation

- **Code change:** extend `coordination/triggers.py` to add a
  `wf-author.decision=fail` evaluator that fires `wf-feedback`,
  carrying the failed validation's rationale + log_excerpt as
  the prompt. Cap to N retries per task per the ADR-0029 pattern.
- **Test:** integration test that a wf-author step with
  `decision=fail` from author-side validation results in a
  wf-feedback dispatch (not silence).

## Notes

- Operator workaround: re-fired via a one-shot
  `_create_and_publish_run` call against the parser task after
  patching the plan's script path. See commit `8b62a8d`.
- Related: this is the second author-side-fail recovery this
  session — also surfaced as task #121's first real-world failure.
- Related: this learning is a candidate for `wf-architecture-resolve`
  since the gap is architectural (missing trigger), not a tactical
  bug in a single line.
