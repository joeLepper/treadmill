---
name: decide
description: Create or update an Architectural Decision Record (ADR) for Treadmill or a Treadmill-managed project. Use whenever a non-trivial design or scope choice is being made - what to build, what to NOT build, which technology to adopt, which alternative to reject, when to supersede a prior decision. Captures context, the decision itself, alternatives considered with rationale, consequences, and an embedded sequence/flow diagram when the decision describes a system interaction. ADRs are the durable memory of why Treadmill is the way it is.
---

# /decide — Architectural Decision Record

An ADR is a single, immutable record of one decision, written when the decision is made. ADRs accumulate; old ADRs are never edited except to flip status (e.g. `accepted` → `superseded by ADR-0017`).

## When to invoke

- A design choice with non-trivial consequences is being settled. "Use Postgres" — yes. "Rename this variable" — no.
- Scope is being decided: in-scope vs out-of-scope for v1.
- A technology, vendor, or external service is being adopted or rejected.
- An invariant is being established (e.g. "tasks are immutable").
- A prior ADR is being superseded or amended.

If the user is exploring alternatives without yet committing, write the ADR with status `proposed` so the discussion is captured even if the decision shifts.

## When NOT to invoke

- Bug fixes, refactors, code-style choices.
- Anything fully derivable from the code (file layout, naming).
- Reversible operational tweaks (timeouts, retry counts) — those go in config + a comment, not an ADR.
- Plans for upcoming work — those go in `docs/plans/`, authored via `/plan`.

## File format

ADRs live at `docs/adrs/NNNN-slug.md` where `NNNN` is a zero-padded four-digit sequence number and `slug` is a kebab-case short title. Numbering is strictly sequential — the next ADR is one greater than the highest existing ADR.

Before writing a new ADR:
1. List `docs/adrs/` to find the highest existing number.
2. Pick the next number.
3. Choose a slug under ~50 characters that's descriptive enough to skim. Bias toward nouns (`local-first-via-moto`) over verbs (`use-moto-locally`).

## Template

```markdown
# ADR-NNNN: Title

- **Status:** proposed | accepted | superseded by ADR-MMMM | amended by ADR-MMMM | rejected
- **Date:** YYYY-MM-DD
- **Supersedes:** ADR-MMMM (omit if not applicable)
- **Related:** ADR-MMMM, ADR-OOOO (omit if not applicable)

## Context

What's the situation? What's true now that makes this decision necessary? What forces are at play (technical, organizational, philosophical)? Stay concrete — quote prior ADRs, code paths, or external constraints.

## Decision

What we are deciding to do, stated plainly. One paragraph, sometimes one sentence. The decision must be falsifiable: a future reader should be able to tell whether the system honors it.

## Alternatives considered

LEAD WITH THE INCUMBENT. The FIRST entry is always the incumbent/native solution — "what do we do today / what does the platform already provide" — with a stated reason it is insufficient. If no convincing insufficiency exists, stop: adopt the incumbent instead of writing this ADR. An ADR without an incumbent-first Alternatives section is incomplete; reviewers reject on that basis (docs/learnings/2026-06-11-check-the-incumbent-before-designing.md — four sessions designed, reviewed, and merged a deploy-approval gate in one night without anyone asking how the previous pipeline handled approval; the answer was a platform-native feature already in use, and the whole surface was reverted).

- **Incumbent: <what exists today>** — the current behavior, the platform's native feature, or the project's own prior art. **Why insufficient:** the concrete gap, stated so a reviewer can disagree.
- **Option name** — one-line description.
- **Why rejected** — the actual reason, not a strawman. If we'd happily reconsider it later under different conditions, say so.

Skipping this section is a smell. Even when an option seems obviously dominant, naming the rejected paths is what makes the ADR durable.

## Consequences

### Good
- What we gain.

### Bad / trade-offs
- What we give up. Be honest — every decision costs something.

### Risks
- What could make us regret this. Optionally: what signal would tell us to revisit.

## Diagram

Include a Mermaid diagram when the decision describes a system interaction, data flow, or state machine. Skip when the decision is purely policy or scope.

### Diagram type by decision class

| Decision class | Diagram kind |
|---|---|
| Actor-to-actor interactions over time | `sequenceDiagram` |
| Static topology / dependencies / layered architecture | `flowchart` |
| Lifecycle and observable state transitions | `stateDiagram-v2` |

### Conformance checklist (per ADR-0004)

A conformant diagram uses named actors only, stays at the intent layer, and labels every interaction. Specifically:

- **Named actors only** — every participant is named explicitly; no `?` participants, no anonymous boxes. "The human" is named.
- **Labels every interaction** with the operation, event, or message name — not a verb. `step.completed` beats "publishes."
- **Stays at the intent layer** — no function signatures, no class names, no return-value details. *What* and *between whom*, not *how*.
- **Uses the right Mermaid kind** for the question, per the table above.
- **Distinguishes synchronous from asynchronous** when it matters (`->>` solid for sync; `-->>` dashed for async/event).
- **Names alternative branches** with `alt`/`else` blocks rather than burying them in prose.

A non-conformant diagram is a defect of the artifact that contains it. Reviewers reject ADRs that ship with vague or decorative diagrams.

\`\`\`mermaid
sequenceDiagram
    actor Human
    participant Treadmill
    participant Worker
    Human->>Treadmill: submit task
    Treadmill->>Worker: dispatch
    Worker-->>Treadmill: report
    Treadmill-->>Human: notify
\`\`\`

## References

- Link to the PRD, plan, or issue that drove this decision.
- Link to ADRs this builds on or depends on.
- External links (papers, vendor docs, manifestos) — only if load-bearing.
```

## Authoring conventions

- **Voice is collective first-person plural.** Use "we" throughout the body. Avoid personal names and individualized framings ("X needs," "the team will") — the ADR records a shared decision, not an individual's preference. Quote individuals only when citing a constraint they imposed; even then, the surrounding analysis is "we."
- **Write in past-completed tense for the decision itself.** "We decided to..." not "We will...". The ADR is the record of a moment.
- **Use absolute dates.** "Today" is meaningless when the ADR is read in two years.
- **Quote, don't paraphrase, when citing constraints.** If a stakeholder said "X is a hard requirement," put it in quotes with attribution.
- **No emojis, no marketing language.** ADRs are evidence, not pitch decks.
- **Default to under 800 words.** Long ADRs hide the decision. If a single ADR seems to need more, it's probably two ADRs.
- **One decision per ADR.** If you find yourself writing "we also decided" — that's a separate ADR.
- **Avoid forward references to future ADRs that don't exist yet.** Instead, list the open questions in a `## Follow-ups` section so a reader sees what's still undecided.
- **Alternatives leads with the incumbent.** Before designing anything, ask "what does the existing system do, and why isn't that sufficient?" — and write the answer down as the first Alternatives entry. Same-frame review never asks this question on its own; the template forces it (docs/learnings/2026-06-11-check-the-incumbent-before-designing.md).

## Reviewer checklist

A reviewer of an ADR (sibling orchestrator, peer worker, or the operator) checks, before anything inside the chosen frame:

- Does the Alternatives section name the incumbent, and is the rejection reason convincing?
- Does the diagram (when present) pass the conformance checklist above?

The first question exists because same-frame review converges within the proposer's frame — it catches flaws inside the design while never challenging the design's premise (docs/learnings/2026-06-11-check-the-incumbent-before-designing.md). Reject ADRs that fail either check; an unconvincing incumbent rejection is a design smell, not a wording nit.

## Status transitions

ADRs are immutable except for the status header. Permitted transitions:

- `proposed` → `accepted` (after agreement)
- `proposed` → `rejected` (we considered it and chose not to do it; keep the ADR for the rationale)
- `accepted` → `superseded by ADR-MMMM` (a new ADR replaces this one wholesale)
- `accepted` → `amended by ADR-MMMM` (a new ADR modifies parts of this one without replacing it)

When superseding or amending, write the new ADR first, then update the old ADR's status header. Do not delete or rewrite the old ADR's body.

## Diagrams as contract

When the ADR includes a sequence diagram, that diagram is the *contract of intent* for any subsequent implementation work. Plans (`/plan`) and tasks should reference the diagram's actors and interactions by name. If implementation diverges from the diagram, either the implementation is wrong or the ADR needs amending — the diagram is not decoration.

## After writing

1. Confirm the ADR file is at the correct path with the correct number.
2. If the ADR supersedes or amends an existing ADR, update that ADR's status header in the same edit session.
3. Tell the user the ADR number and one-line summary so they can reference it in future conversation.
4. Do NOT commit, push, or publish — that's the human's call.
