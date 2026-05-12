---
date: 2026-05-07
trigger: correction
status: crystallized-into-rule-purpose-before-collapse
related: ADR-0003, 2026-05-08-per-role-images-collapse-attempt
---

# Learning: Treat structural separators as load-bearing until proven otherwise

## Trigger

While drafting ADR-0003, we authored a separate `docs/knowledge-base/` subdirectory for the cross-project knowledge base. On a vague-feeling reviewer comment ("the note doesn't match the reality of the repo"), we collapsed the subdirectory into Treadmill's main `docs/` tree, distinguishing internal vs. cross-project artifacts by frontmatter alone. The user pushed back: *"hold on. I think we should back that out. I now see why you had that knowledge-base subdirectory before. You were using it to disambiguate from the docs that we want to apply to the projects that treadmill works on from the docs that we write about treadmill itself as we develop it."*

We restored the disambiguation, this time naming the audience boundary explicitly in the ADR.

## Observation

When the orchestrator encounters a structural separator that "feels superfluous" — two directories, two ADR sequences, two skill names where one might do — the path of least friction is to consolidate. We took that path. The separator turned out to be load-bearing on an axis (audience) that wasn't visible from inside the orchestrator's recent context.

The collapse was not technically wrong; it was *under-considered*. We optimized for "fewer concepts" without examining whether the existing concept count was solving a real problem.

## Generalization

We tend to read structural complexity as accidental and reach for consolidation. In a system whose entire purpose is to crystallize hard-won distinctions, this default is corrosive. Before consolidating any visible separator (directories, layers, distinct artifact types, parallel sequences), we should ask: *what audience or lifecycle does this separator serve, and what breaks if I dissolve it?* The answer is sometimes "nothing" — but the question must be asked, not skipped.

A sub-pattern worth flagging: when a user comment is short and unspecific, the orchestrator's default should be *clarify before acting*, not infer the maximally-helpful interpretation. A wrong inference here cost a round-trip that a one-line clarifying question would have prevented.

## Proposed rule

Before collapsing a structural separator (directory, layer, parallel sequence, distinct artifact type), the orchestrator must explicitly name the separator's apparent purpose and confirm with the human that the purpose has dissolved. Mere "this seems redundant" is not sufficient justification.

## Proposed remediation

Two layers, hybrid:

1. **Process.** Author a candidate amendment as `proposed`, not `accepted`, when the change collapses an existing separator. Force a review round before acceptance.
2. **LLM judge.** When an ADR amendment removes a directory, a layer, or a parallel artifact sequence, an LLM judge inspects the diff for a `## Why this separator no longer serves` section. Absent or unconvincing → flag for human review.

Both depend on the rule engine that ADR-0006 will scope.

## Notes

This learning would have been auto-surfaced by the ADR-0008 hook had it been live: the user's prompt contained "hold on" and "back that out" — both in the trigger phrase list. We are capturing it manually here as the bootstrap; from the next session forward, the hook does the surfacing and the orchestrator authors the learning.

The companion learning at `2026-05-07-repo-relative-path-conventions.md` records the smaller, mechanical mistake that preceded this conceptual one.
