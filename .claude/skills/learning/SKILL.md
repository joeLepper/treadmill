---
name: learning
description: Capture a learning — a moment where the work taught us something worth keeping. Use when something surprised us, when a correction was issued, when a pattern repeated, when a PR drew strong feedback, or when a plan veered. Learnings are the raw input that crystallizes (later) into rules and remediations applied across all Treadmill-managed projects. The schema here is designed to migrate cleanly into Treadmill's durable knowledge base when that exists.
---

# /learning — Capture a learning

A learning is a small, structured record of something we noticed during the work. It is not a decision (that's an ADR) and not a plan (that's a plan doc). It is the raw observation. Learnings accumulate; periodically we crystallize them into rules and remediations.

## When to invoke

- A correction was issued in conversation and the substance is reusable beyond the moment.
- A surprise hit during execution — something didn't go as expected, or an assumption broke.
- A pattern repeated across two or more incidents and is worth naming.
- A PR or change drew strong reactions (positive or negative) that contain reusable signal.
- A plan veered off its declared scope and the *why* is worth keeping.

Bias toward capturing. A learning is cheap; missing one means losing signal.

## When NOT to invoke

- A one-off bug fix with no general lesson — fix it, move on.
- Routine work that confirms what we already know.
- Personal opinion without specific evidence — wait until it has a referent.

## File format

Learnings live at `docs/learnings/<date>-<slug>.md` where `<date>` is the date the learning was captured (`YYYY-MM-DD`) and `<slug>` is a kebab-case short title under ~50 characters. Slugs should be noun-y descriptions of the learning itself ("features-ship-with-tests"), not the incident that triggered it.

Multiple learnings on the same day are fine. Date plus slug must be unique.

## Schema

```markdown
---
date: YYYY-MM-DD
trigger: correction | surprise | pattern | feedback | plan-drift | other
status: captured | crystallized-into-ADR-NNNN | crystallized-into-rule-<slug> | obsolete
related: ADR-NNNN, plan-YYYY-MM-DD-slug   # optional
---

# Learning: Title

## Trigger
What happened that prompted this capture. One paragraph. Concrete: name the incident, link to the artifact (commit, PR, plan section, conversation excerpt) that triggered it.

## Observation
What we noticed. State the fact, not the explanation. "We shipped Day 1 of the spike with no tests" — not "we should write tests."

## Generalization
The reusable lesson. If we keep this learning, what does it tell us about future work? Express as a tendency or pattern, not a rule yet ("we tend to prioritize forward motion over verification when momentum is good").

## Proposed rule
A candidate rule statement, if there's an obvious one. Keep it terse and falsifiable. Mark `none` if the learning isn't ready to be a rule yet.

## Proposed remediation
What should happen when the rule is violated, if there's an obvious answer. Could be: a deterministic check (e.g., "PR check fails if no test files changed"), a non-deterministic check (e.g., "LLM-as-judge inspects PR for test coverage of changed surface"), an auto-task (e.g., "open a follow-up to add tests"), or a human ping. Mark `none` if not yet clear.

## Notes
Optional. Anything else worth keeping — links, transcript excerpts, related learnings.
```

## Authoring conventions

- **Voice is collective first-person plural.** Same convention as ADRs and plans.
- **One learning per file.** If you find yourself writing about two distinct lessons, write two files.
- **Default to under 250 words.** Learnings are evidence, not essays. The crystallization step is where weight is added.
- **The trigger field carries proof.** If we cannot point at a concrete incident, we are speculating, not learning.
- **Status starts at `captured`.** Move to `crystallized-into-...` only when an ADR or rule actually references this learning.
- **No emojis, no marketing language.**

## Auto-capture

Per ADR-0008, a Claude Code `UserPromptSubmit` hook surfaces correction-phrase triggers as candidate learnings without requiring manual `/learning` invocation. The hook:

- Scans every user prompt against `tools/dev-hooks/learning_triggers.json`.
- On match, appends a JSONL record to `.treadmill-local/learning-candidates.jsonl` with timestamp, matched phrase, and session pointer.
- Emits an `additionalContext` injection: *"a correction-phrase trigger fired; consider whether this moment warrants a `/learning` capture."*

The hook is advisory. When you (the orchestrator) see the injection, your job is:

1. **Decide.** Does this moment warrant a learning? False positives are cheap; missed learnings are expensive — bias toward yes.
2. **Author** a learning file via this skill.
3. **Flip the queue entry's `status`** in `.treadmill-local/learning-candidates.jsonl` from `open` to `captured` (or `dismissed` if you decide no learning is warranted). The queue is JSONL — append a new line with the updated record, or rewrite the line in place; both are acceptable.

Future triggers (deferred but covered by the same queue):

- A plan transitions to `abandoned` → wrapper script writes a candidate referencing the post-mortem.
- A PR receives harsh review feedback → webhook writes a candidate (requires GitHub integration).
- The runtime gains its own observability hooks → richer signal sources.

All triggers append to the same queue. The schema in this file is the contract every trigger source writes against.

## Session-end review

Open candidates that have not been dispositioned by session end deserve a sweep before exiting. If `cat .treadmill-local/learning-candidates.jsonl | jq 'select(.status == "open")'` returns entries you have not seen, address them. Either author a learning or mark the entry `dismissed` with a one-line reason in a `notes` field.

## After capturing

1. Confirm the file is at the right path with a unique slug.
2. Tell the user the learning's slug and one-line summary.
3. If the learning has a clear proposed rule and remediation, suggest authoring a rule (when the `/rule` skill exists). Do not author rules unilaterally — rules apply across all projects, so they cross a higher bar than learnings.
