# docs/knowledge-base — what Treadmill ships to managed projects

## What is this directory?

`docs/knowledge-base/` is the cross-project surface — Layer 3 in ADR-0003. The artifacts here are written for *managed projects* and the agents that operate within them, not for the people building Treadmill itself. The audience boundary is the reason this tree is separate from `docs/`.

Subdirectories:

- `adrs/` — cross-project policy ADRs. Decisions Treadmill makes about how managed projects must work (e.g., "every managed project must have an `AGENTS.md` at every significant directory"). Numbered independently from Treadmill's internal ADRs at `docs/adrs/`.
- `learnings/` — crystallized cross-project learnings. Frontmatter `kind: crystallized` plus `crystallized_from: [...]` referencing source learnings (in `docs/learnings/` or in any managed project's repo).
- `rules/` — formalized constraints with attached remediations. Schema and engine come in ADR-0006.

## What conventions apply here?

**Audience first.** Every artifact here is *for managed projects*. If something is about how Treadmill itself is built, it goes in `docs/`, not here.

**Citation convention** (interim, until the first cross-project ADR forces us to lock it in): when ambiguity is possible, internal ADRs are cited as `ADR-NNNN` and cross-project ADRs as `KB-ADR-NNNN`. ADR-0003 lists this as a pending decision.

**Promotion is authoring, not moving.** When a Treadmill-internal learning generalizes, we author a NEW crystallized learning here referencing the source via `crystallized_from:`. The source learning stays put in its origin repo as raw evidence.

## What should an agent read first?

When the directory is populated, this list will point at the most relevant cross-project policy ADRs and rules. For now: nothing has been authored here yet. The first content is expected to come from ADR-0008 (auto-capture, including the schema for crystallized learnings) and ADR-0006 (rules + remediations).
