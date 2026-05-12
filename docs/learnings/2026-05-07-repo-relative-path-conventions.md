---
date: 2026-05-07
trigger: correction
status: captured
related: ADR-0003
---

# Learning: Default to repo-relative paths in repo-internal documentation

## Trigger

In ADR-0003 (drafted in the Treadmill repo), we wrote the interim location of the cross-project knowledge base as `treadmill/docs/knowledge-base/{learnings,rules,adrs}/`. The user flagged it: *"the note that you made on the interim location doesn't match the reality of the repo at present."* The `treadmill/` prefix would only make sense from outside the repo; from inside (which is where the doc is read), the path is `docs/knowledge-base/...`.

## Observation

We added a project-name prefix to a path inside the project's own documentation. The other paths in the same ADR (`docs/adrs/`, `docs/plans/`, etc.) were correctly written without the prefix. The inconsistency was local to one bullet — a small slip rather than a misunderstanding of the convention.

## Generalization

The orchestrator drifts when it switches between two reading contexts mid-sentence: "what would the author of this doc see?" vs. "what would a reader from outside see?" Repo-internal documentation is read from inside the repo; paths in it should be repo-relative without exception. Tooling can enforce this; the orchestrator can also self-check.

## Proposed rule

In any artifact authored inside a repo's own `docs/` tree, file-system path references resolve from the repo root, not from any parent. A path beginning with the repo's own name (`treadmill/docs/...` inside the Treadmill repo) is by definition wrong.

## Proposed remediation

1. **Deterministic.** A pre-merge check (eventually a hook) greps every markdown file under `docs/` for path references beginning with the repo's own name. Match → fail.
2. **LLM judge** is unnecessary; the rule is mechanical.

## Notes

Companion to `2026-05-07-collapse-then-restore.md`, which records the conceptual error that grew out of this session's path-prefix correction. The path mistake was small; the user's response surfacing it gave us the opportunity to think harder about the conceptual model — and we then made a larger mistake. Capturing both as separate learnings keeps the lessons independently addressable.
