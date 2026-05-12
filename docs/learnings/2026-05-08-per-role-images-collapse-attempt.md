---
date: 2026-05-08
trigger: correction
status: crystallized-into-rule-purpose-before-collapse
related: 2026-05-07-collapse-then-restore, ADR-0001
---

# Learning: Per-role Docker images — second instance of the collapse-then-restore pattern

## Trigger

While analyzing bunkhouse's task/workflow/role hierarchy for Treadmill (in preparation for ADR-0010), we proposed dropping per-role Docker images on the rationale that "dynamic role config (model + prompt + skills + hooks) makes per-role images redundant — one base image plus dynamic specialization is cleaner." The user pushed back: *"We will definitely want tiers. One of the things that we need to be able to do that we're not considering right now, is run ML workloads via Treadmill. I think that might also be why we had role-specific docker images, as well. … Research more about the role image before we commit to dropping it."*

The proposed collapse was based on what we *could see* (dynamic config replicates the prompt-level specialization), not on what we *hadn't researched* (per-role images may carry workload-specific dependencies — CUDA, GPU drivers, ML frameworks — that no amount of dynamic config replicates).

## Observation

Same pattern as `2026-05-07-collapse-then-restore`: structural complexity read as redundancy without research into the purpose the complexity served. There, the missed purpose was *audience boundary* (Treadmill-internal vs. cross-project docs). Here, the missed purpose is *workload specialization* (ML deps in the image, not the prompt).

Two distinct domains (documentation, infrastructure) — same orchestrator misread.

## Generalization

The pattern is now twice-observed and abstracted. The orchestrator defaults to consolidation when it sees structural complexity, and the cost of being wrong is a round-trip with the human. The pattern crystallizes cleanly into a rule because the remediation is mechanical: before proposing to remove a structural separator, document what purpose the separator served and confirm with the human that the purpose has dissolved.

The "purpose" question is unfamiliar to read off code alone. It often requires research — what was this serving when it was authored, what would break if it disappeared, what loads bear on it that aren't visible in the current trace.

## Proposed rule

A new rule, `rule:purpose-before-collapse`, formalizes this. It applies whenever the orchestrator proposes to remove a directory, layer, parallel artifact sequence, distinct artifact type, infrastructure separator, or any visible structural complexity.

## Proposed remediation

LLM-judged on the proposing artifact (ADR draft, plan section, conversation message). The judge looks for a "What purpose this complexity served" subsection or equivalent reasoning. Absent or hand-wavy → flag for human review before acceptance.

## Notes

This learning was surfaced by the ADR-0008 hook firing on "i don't think" — technically a false positive on the trigger phrase (the user was agreeing, not correcting), but the actual correction in the same message is the per-role-image moment. The hook's bias toward firing was correct overall: it created the pause that let me notice the pattern. False positive on substring; true positive on signal.

This is the kind of corroboration that the `/rule` skill explicitly names: "Authoring a rule from a pattern that has been independently observed in multiple sessions is usually right."
