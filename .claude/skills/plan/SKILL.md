---
name: plan
description: Create or update a Plan document for an upcoming changeset, spike, or epic. Use whenever non-trivial work is about to start and we need to record the goal, success criteria, scope (including what's out), sequence, and risks before authoring code or tasks. Plans are the bridge between ADRs (decisions) and work (commits, tasks, PRs). Plans are mutable while in flight and gain a post-mortem when they complete.
---

# /plan — Plan a changeset, spike, or epic

A plan is the operational artifact that turns decisions (ADRs) into work. It is short, skimmable, and concrete. Where ADRs are immutable records of moments, plans evolve while work is in flight and end with a post-mortem.

## When to invoke

- Multi-step work is about to start and the sequence matters.
- A spike, refactor, or feature with non-obvious scope is being committed to.
- Decisions have been made (one or more ADRs) and we need to operationalize them.
- Multiple sessions or contributors will work on this and need a shared reference.

## When NOT to invoke

- Single-task changes that fit in one PR — describe them in the PR body.
- Pure exploration with no commitment — talk it out, don't bureaucratize it.
- Decisions that haven't yet been made — those belong in an ADR via `/decide`.

## File format

Plans live at `docs/plans/<date>-<slug>.md` where `<date>` is the authoring date (`YYYY-MM-DD`) and `<slug>` is a kebab-case short title under ~50 characters.

Plans are not numbered. They are scoped by date and slug. Multiple plans on the same day are fine — date plus slug should be unique.

## Template

```markdown
# Plan: Title

- **Status:** drafting | active | completed | abandoned | superseded by <date>-<slug>
- **Date:** YYYY-MM-DD
- **Related ADRs:** ADR-MMMM, ADR-OOOO (omit if not applicable)
- **Supersedes:** <date>-<slug> (omit if not applicable)

## Goal

What we are trying to accomplish, in plain language. One short paragraph.

## Success criteria

Concrete, testable outcomes. A reader should be able to look at the system at the end and judge each criterion as met or not met. Avoid "it works" — name the observable behavior.

## Constraints / scope

### In scope
What we will do.

### Out of scope
What we will not do, even if tempted. This list is required, not optional — naming what we will not do is half of scope discipline.

### Budget
Time, headcount, or token budget. If the budget is exhausted before success criteria are met, we abort and write a post-mortem rather than escalate quietly.

## Sequence of work

A short, ordered list of work units. Each unit is sized to roughly one day of focused work. If a unit feels bigger, split it. Mark dependencies inline.

## Diagram (if applicable)

A Mermaid sequence or flow diagram showing the intended end-state. Skip when the plan is purely organizational. When the diagram already exists in a related ADR, reference the ADR rather than duplicating it.

## Risks / unknowns

What could derail this. What we'll learn during execution that might invalidate the plan. Pair each risk with a mitigation or a "we'll abort if" trigger.

## Decisions captured during execution

A running list, populated as we work. When a real decision emerges (one that future readers need to understand), link to the ADR authored via `/decide`. This section starts empty and is appended to in place.

## Post-mortem

Filled in when the plan transitions to `completed` or `abandoned`.

- **What worked.** Briefly.
- **What surprised us.** Honestly.
- **What should become an ADR, learning, or rule.** Pointers to follow-up artifacts.
- **What this plan teaches us about future plans.** Process feedback.
```

## Authoring conventions

- **Voice is collective first-person plural.** Use "we" throughout. Same convention as ADRs.
- **Default to under 500 words.** Plans should be skimmable. If a plan needs more, the scope is probably too big — split it.
- **Reference ADRs; do not restate them.** If a plan needs to explain *why* the work is happening, link to the ADR.
- **Success criteria must be observable.** "The adapter handles autoscaling" is unmeasurable; "publishing 3 messages results in 3 workers spawning sequentially and 0 workers after the idle window" is measurable.
- **The Out-of-scope list is required.** A plan without explicit non-goals will accrue them silently.
- **Tasks size to ~1 day.** Sequencing reads cleanly; estimate accuracy is acceptable.
- **No emojis, no marketing language.** Plans are operational, not promotional.

## Status transitions

- `drafting` → `active` when work starts.
- `active` → `completed` when success criteria are met.
- `active` → `abandoned` when we stop work without meeting the criteria. The post-mortem is mandatory.
- `active` → `superseded by <date>-<slug>` when a new plan replaces this one. The replacing plan must reference this one.

While `drafting` or `active`, the plan body is mutable — update freely. After `completed` or `abandoned`, edits are limited to status changes and post-mortem additions; the body of the plan is preserved as the historical record of intent.

## After execution

1. Update the status header.
2. Write the post-mortem honestly. Surprises and failures are more valuable than what worked.
3. Author follow-up artifacts: ADRs for new decisions, learnings for new patterns, rules for new constraints. The plan's post-mortem links to them.
4. Tell the user what's done and what artifacts the plan produced.
