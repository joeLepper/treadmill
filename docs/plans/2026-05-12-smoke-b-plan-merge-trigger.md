---
status: active
trigger: smoke-test for ADR-0021 (plan-merge-to-main as submission trigger)
parent: docs/plans/2026-05-13-week-4-dev-local-deployment.md
---

# Smoke B: prove the plan-merge-to-main trigger

A trivial test of ADR-0021. Treadmill should detect this plan doc
merging to `main`, fetch the doc, parse the frontmatter (see the
`status: active` marker above), and dispatch the task below
**without any operator CLI invocation**.

If you're reading this in `main`, the dispatch should have already
happened — the merge of this file was the kickoff. If everything
worked end-to-end, there should also be a Treadmill-authored PR
adding `docs/SMOKE_B.md` to the repo, opened by the
`role-code-author` worker that the autoscaler spawned in response
to this plan's task dispatch.

The only manual steps in this loop are: write the plan, open the
PR, review, approve, merge. From the merge forward, Treadmill
handles itself.

## sequence_of_work

```yaml
sequence_of_work:
  - id: write-smoke-b-marker
    title: Add docs/SMOKE_B.md marker file
    workflow: wf-author
    intent: |
      Create the file ``docs/SMOKE_B.md`` at the repository root
      under ``docs/``. The file should contain exactly two lines:

      1. ``Smoke B: ADR-0021 plan-merge-to-main trigger proved end-to-end.``
      2. The current date in ``YYYY-MM-DD`` form.

      The file is a marker that this plan was picked up automatically
      by Treadmill from the merge of its own plan doc. No edits to
      other files are required.
    scope:
      files:
        - docs/SMOKE_B.md
    validation:
      - kind: deterministic
        description: |
          ``docs/SMOKE_B.md`` exists at the path and is non-empty
          after the task completes.
```
