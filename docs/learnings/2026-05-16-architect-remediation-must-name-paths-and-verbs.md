---
date: 2026-05-16
trigger: pattern
status: captured
related: ADR-0032, ADR-0038, ADR-0040
last_crystallization_check: 2026-05-17
crystallization_backoff_until: 2026-05-24
crystallization_target: pending-second-instance
---

# Learning: Architect amend verdicts must name specific paths and verbs

## Trigger

During the 2026-05-16 hands-free session (ADR-0040 smoke), architect `amend`
verdicts were emitting `remediation_summary` fields too abstract for `wf-plan`
to act on. Representative examples from the session:

- "The implementation is incomplete. The validation runner is not wired into
  the author disposition." — no file path, no function, no verb.
- "The missing pieces from the spec need to be added to the relevant module."
  — no anchor, no diff target.

`wf-plan` dispatched from these summaries and produced plans with tasks that
mirrored the same abstraction: "wire validation runner into author disposition."
The resulting wf-author runs branched and produced code that did not match the
original spec because the plan carried no file:line anchor. The downstream
validator then rejected the work, creating an architect → plan → author →
validate → architect cycle that repeated the miss.

## Observation

The `ArchitectVerdict.remediation_summary` field (ADR-0032) is the spec that
`wf-plan` converts into concrete tasks. When the summary is abstract, the
generated plan is abstract, and the tasks are guessing. The imprecision
compounds across the chain:

> architect vague → plan vague → author misses the target → validator rejects
> → architect arbitrates again

The pattern in affected verdicts: the architect correctly identifies *that*
something is missing (`amend`) but expresses the missing thing as intent
rather than code anatomy — "validation should be run" instead of "in
`workers/agent/treadmill_agent/runner_dispositions/code.py`, add a call to
`run_validation_block(task)` before `git push` at line ~210."

This mirrors the `2026-05-08-fabricated-supporting-evidence` failure mode in
reverse: there, the orchestrator added precision that wasn't warranted; here,
the architect omits precision that is required. Both produce artifacts that
look authoritative but aren't actionable.

## Generalization

An architect `amend` verdict's `remediation_summary` must name at minimum:

1. **Which file(s)** need to change (repo-relative paths).
2. **Which function, class, or section** within those files is the target.
3. **What operation** is needed: add, remove, replace, rename, wire, extract.
4. Optionally: **which spec clause** the missing piece traces to (for auditability).

A summary that satisfies these four points is actionable by `wf-plan` without
re-reading the original task spec. A summary that doesn't satisfy them will
generate a plan that is equally vague.

The test: could a new orchestrator, reading only the `remediation_summary`,
write the correct code without the original task spec? If not, the summary
is not specific enough.

## Proposed rule

An LLM-judge check that evaluates architect `amend` verdicts and requires
`remediation_summary` to include at least one repo-relative file path
(matches `[a-z][\w/.-]+\.(py|yaml|ts|sh)`) and at least one imperative verb
(add, remove, replace, rename, wire, extract, delete, insert, wrap) adjacent
to a code element name. Severity: warning (imprecise summary is better than
no summary; we don't want to block all amend verdicts, just surface the gap).

## Proposed remediation

1. **Prompt update (immediate):** role-architect's system prompt adds a
   concrete example showing a good vs. bad `remediation_summary`. Commit
   `cdf5451` (2026-05-16) began tightening architect output discipline for
   `validator_tuning`; the same pattern applies to `remediation_summary`.
2. **LLM-judge rule (deferred):** once the rule engine can evaluate architect
   step outputs, add the judge described above. The deterministic pre-check
   (does the summary contain a file path substring?) is cheap to add inline
   in the disposition before the judge runs.

## Notes

Single observed pattern; below rule threshold per `/rule`. Watch for a second
instance before crystallizing the LLM-judge rule. The prompt update (item 1
above) will reduce future frequency; if imprecise summaries recur after the
prompt change, crystallize immediately without waiting for a third instance.

Related: `2026-05-08-fabricated-supporting-evidence` — both are precision
calibration failures in architect/orchestrator output. That learning says
"don't add precision you can't source"; this one says "don't omit precision
that downstream roles need."
