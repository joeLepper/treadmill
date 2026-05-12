---
status: active
trigger: smoke-test for ADR-0021 (take 2; first attempt blocked on missing GITHUB_TOKEN in API container env, fixed in commit 8b0f63c)
parent: docs/plans/2026-05-13-week-4-dev-local-deployment.md
---

# Smoke B (take 2): prove the plan-merge-to-main trigger

The first attempt at this smoke (`2026-05-12-smoke-b-plan-merge-trigger.md`,
merged as PR #4) demonstrated that the `pr_merged` event reached the
events table but the plan-doc handler silently no-op'd because
`GITHUB_TOKEN` wasn't injected into the API container's env. The
local-adapter now fetches the PAT from Secrets Manager on `up` and
injects it via env var. This plan re-tests the chain end-to-end.

Treadmill should detect this plan doc merging to `main`, fetch the doc
via the now-wired `github_client`, parse the `status: active`
frontmatter, and dispatch the task below without any operator CLI
invocation.

## sequence_of_work

```yaml
sequence_of_work:
  - id: write-smoke-b-marker-take-2
    title: Add docs/SMOKE_B.md marker
    workflow: wf-author
    intent: |
      Create the file ``docs/SMOKE_B.md`` at the path
      ``docs/SMOKE_B.md`` (under the docs directory at the repository
      root). The file should contain exactly two lines:

      1. ``Smoke B take 2: ADR-0021 plan-merge-to-main trigger
         confirmed end-to-end.``
      2. The current date in ``YYYY-MM-DD`` form.

      No edits to any other file. Just create this single marker.
    scope:
      files:
        - docs/SMOKE_B.md
    validation:
      - kind: deterministic
        description: |
          ``docs/SMOKE_B.md`` exists at the path and is non-empty
          after the task completes.
```
