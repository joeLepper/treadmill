---
date: 2026-05-11
trigger: correction
status: captured
related: ADR-0003, ADR-0010, plan:2026-05-08-minimum-runnable-treadmill, plan:2026-05-11-week-2-closure
---

# Learning: Plans go in durable files, never in orchestrator context or TODO lists

## Trigger

Discussing scope for Week 3 + the design work that needs to land first (mergeable concept, multi-step workflows, uniform output envelope), the orchestrator soft-pushed-back on bloating scope. The user agreed scope was going to bloat but flipped the concern: *"I'm fine in bloating our scope so long as we're writing our plans down in a durable place. That's the key learning from bunkhouse that I want to get right here. Planning in ephemeral places (like your context or todo lists) doesn't work well. Especially on things that span orchestrators or multiple days."*

The orchestrator's framing — "Week 3 might need to scope down" — implicitly assumed the alternative was carrying additional scope in context (next-week's TODO list, oral handoff). The user reframed: scope-bloat is *fine if persisted to disk*; the failure mode isn't scope size, it's plan-evaporation when the orchestrator's context resets or a different orchestrator takes over.

## Observation

Two orthogonal axes that the orchestrator was conflating:

- **Scope size**: how much work the plan covers.
- **Plan durability**: whether the plan survives outside this orchestrator's context.

The orchestrator's default failure mode is to treat the *current conversation* as the persistence layer. Tasks via TaskCreate are conversation-scoped. Items mentioned in chat decay with context window. Even decisions made in this turn are at risk of being lost when summarization fires and the orchestrator reconstructs from a compressed history.

The user has been protecting against this throughout the project — every meaningful decision lands in `docs/adrs/`, every milestone plan lands in `docs/plans/`, every learning lands in `docs/learnings/`, every captured rule lands in `docs/knowledge-base/rules/`. The pattern is *write it down where the next orchestrator can find it without the prior session's context*.

This is the inversion of the rule that the orchestrator might naturally apply to a human collaborator: *"don't bother writing down what's in working memory."* For orchestrators, working memory is fragile and the cost of writing-it-down-anyway is cheap. The asymmetry matters.

## Generalization

When the orchestrator is about to commit to scope, decisions, or sequencing, the durability check fires first:

> Is this going to live somewhere the next orchestrator (or this orchestrator after a context reset) can find without re-deriving? If not, write it down before proceeding.

Concrete classes that *must* be durable:

- Multi-day work plans (lands in `docs/plans/<date>-<slug>.md`).
- Design decisions that constrain future work (lands as ADR).
- Captured failure patterns (lands as learning).
- Crystallized rules (lands in `docs/knowledge-base/rules/`).
- Important context the next orchestrator needs to know about the user, the system, or both (lands in cross-conversation memory at `~/.claude/projects/.../memory/`).

Conversation-scoped only:

- TaskCreate / TaskUpdate for active in-conversation tracking.
- Skill / agent inputs that get persisted by the tool itself.
- Ephemeral question-and-answer state that doesn't outlive the immediate exchange.

The trap is *near-durable* commitments — "I'll do X next" said aloud, "we agreed Y" left implicit. These are the highest-loss when context resets, because the orchestrator doesn't realize they were ever there.

## The bunkhouse precedent

The user named this as *the* key learning from bunkhouse. Bunkhouse's failure mode was planning in ephemeral channels (Slack threads, oral handoffs, the orchestrator's working memory) and discovering, days later, that the plan had drifted because no one had a canonical copy. Treadmill is built to invert that — the plan-doc is a first-class entity in ADR-0010, the running log is the durable trace, ADRs are append-only.

The orchestrator's role in this is *making sure plans land on disk before the conversation closes*, not just *participating in the planning*.

## Proposed rule

A candidate. The user has been applying this discipline consistently since Phase 2 began; this is the first time it was named explicitly. Watch for one more instance before crystallizing.

If a second instance arrives, the rule shape is something like:

> *Before ending a substantial planning exchange, the orchestrator writes the plan to a durable file. TODO lists, TaskCreate items, and chat-resident commitments do not count as durable. The cost of writing-it-down is cheap; the cost of plan-evaporation across context resets is asymmetrically high.*

## Proposed remediation

None yet — wait for the rule. But the practical application is immediate: before firing the adversarial researcher on the mergeable problem, capture this learning + memory. Before writing more design exchanges with the user about workflows, write the ADRs (or at least their stubs) that those exchanges will produce. The plan that slots ahead of Week 3 lands as a `docs/plans/<date>-<slug>.md`, not as a chat agreement.

## Notes

The auto-capture hook caught "i don't think" three times in the user's message. Different concerns, same correction shape: the user is *consistently* applying a more disciplined planning posture than the orchestrator defaults to.

This learning pairs with `2026-05-11-uniform-output-shape-over-per-workflow-typing.md` and `2026-05-08-commodity-vs-architectural-decision-weight.md` — three instances now of the meta-principle *"is this already answered? is this already written down? is this already settled by bunkhouse precedent?"* The orchestrator's bias is to derive fresh; the user's discipline is to reach for the existing answer first.
