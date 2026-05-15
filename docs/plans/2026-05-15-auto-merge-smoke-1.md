---
status: drafting
trigger: ADR-0031 first-auto-merge smoke. Drives the parser+trigger+dedup pipeline end-to-end against a real trivial PR.
---

# Plan: Auto-merge smoke 1 (default-enabled)

## Goal

Prove ADR-0031 auto-merge fires end-to-end on a trivial change against this default-enabled plan (no ``auto_merge`` frontmatter — should default to enabled).

## Success criteria

- The single task below dispatches wf-author, the agent opens a PR, wf-review approves it, wf-validate passes, mergeability flips to ``mergeable``, the 30s cooling-off window elapses, and the auto-merge trigger merges the PR with no operator intervention.

## Constraints / scope

### In scope
- One trivial task: append a single line to `docs/handoffs/2026-05-15-auto-merge-smoke-notes.md` confirming the smoke ran.

### Out of scope
- Anything else.

### Budget
- One smoke cycle.

## Sequence of work

```yaml
sequence_of_work:
  - id: smoke-touch
    title: Append smoke marker line
    workflow: wf-author
    intent: |
      Create or append to ``docs/handoffs/2026-05-15-auto-merge-smoke-notes.md``
      a single line of the form:

          ``Smoke 1 (default-enabled) — observed at <timestamp UTC>.``

      That is the entire change. Do not add any other content. Do not
      modify any other files.
    scope:
      files:
        - docs/handoffs/2026-05-15-auto-merge-smoke-notes.md
    validation:
      - kind: deterministic
        description: |
          Smoke marker file exists and contains "Smoke 1".
        script: |
          test -f docs/handoffs/2026-05-15-auto-merge-smoke-notes.md \
            && grep -q "Smoke 1" docs/handoffs/2026-05-15-auto-merge-smoke-notes.md
```
