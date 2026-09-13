---
date: 2026-09-13
trigger: surprise
status: captured
related: ADR-0105, ADR-0107
---

# Learning: /plan step 4 lets an author defer scope gaps the gates require

## Trigger
Dogfooding the new review panel on `.claude/skills/plan/SKILL.md` (the panel now IS
the adversarial-review mechanism, ADR-0105/0107), the GPT reviewer raised a blocking
finding about PRE-EXISTING skill text — outside the diff that rewired the review path.

## Observation
`/plan` "Before submitting to Treadmill" step 4 says scope gaps may be "explicitly
defer[red] with a note in the Risks section." But the Authoring conventions require
each code task to scope in its touched files, its existing tests, and the component
`AGENT.md` (the docs-current gate is BLOCKING), and note that `wf-ci-fix` cannot reach
files outside scope. So a plan can be submitted with a deferred scope gap that then
structurally trips a blocking gate downstream, leaving a worker unable to fix a
required file within scope.

## Generalization
A skill that both (a) enumerates gate-required scope and (b) offers a general "defer
with a note" escape hatch will let authors defer exactly the things the gates cannot
tolerate. An escape hatch must exclude the requirements that are gate-enforced.

## Proposed rule
The step-4 defer allowance should exclude gate-required scope: touched files, their
existing tests, and the component AGENT.md/agent-changes fragment must never be
deferred — only genuinely optional scope may be deferred with a Risks note.

## Proposed remediation
Amend `/plan` step 4 to say scope gaps that a blocking gate (docs-current,
tests-in-scope) would trip may NOT be deferred; only non-gate scope may. Small skill
edit; not done here to keep the review-mechanism rewiring scoped.

## Notes
Found by the panel's GPT leg; confirmed pre-existing (the text is unchanged in the
rewiring diff). Sibling (Ernie) agreed it is out of scope for the mechanism swap and
should be filed, not held.
