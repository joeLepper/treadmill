---
status: active
trigger: ADR-0044 (datetime IDs) closes the concurrent-author collision class but leaves copy-paste duplication unguarded. ADR-0045 (this plan's parent) decides that the gate goes in pre-merge CI on the `pull_request.merge` ref.
parent: docs/adrs/0045-alembic-heads-ci-gate.md
---

# Plan: Alembic head-multiplicity CI gate (ADR-0045 execution)

Ship the one-step CI gate that asserts `alembic heads` returns exactly one line, plus a smoke test that proves the gate fires when a colliding revision lands.

## Goal

After execution: a PR that introduces a second alembic head (whether by accident or copy-paste) fails CI with a clear error naming the conflicting revision IDs, blocking merge.

## Success criteria

- `.github/workflows/ci.yml` includes a step that runs `alembic heads | wc -l | xargs -I{} test {} -eq 1` (or equivalent) under the `services/api` job, as a required check.
- A negative-test smoke PR (intentional collision) is opened, observed to fail CI on the new step, and closed without merging — documented in a handoff.
- Branch protection is configured to require the new check before merge.

## Constraints / scope

### In scope
- The CI step + test.
- The smoke test handoff doc.
- One sentence in `docs/runbooks/` (or AGENT.md) pointing at the new check for future operators.

### Out of scope
- Backfilling missing revision metadata on historical migrations (none missing today).
- Runtime detection in the API of multi-head state (logging is sufficient; CI gate is the structural defense).
- Migration-content linting (e.g., requiring docstrings, asserting transactional safety). Separate concern.

### Budget
1 day. If it slips past 2, the check is too complex for what it is — abort and reconsider.

## Risks / unknowns

- **Branch protection requires admin to configure** — the workflow change ships in this plan; turning the check into a *required* check is an operator action. Plan accepts that the smoke test demonstrates the gate's failure mode, and operator-side branch-protection update is documented in the handoff.
- **The merge ref is not always available** — for PRs from forks, GitHub Actions restricts the merge ref. Treadmill is a single-org repo; this is theoretical. Documented as a future consideration if/when the repo opens to forks.

## Sequence of work

```yaml
sequence_of_work:
  - id: ci-gate-alembic-heads
    title: Add `alembic heads` CI step under services/api job
    workflow: wf-author
    intent: |
      Edit ``.github/workflows/ci.yml`` to add a step under the
      ``services/api`` job, immediately after ``uv sync`` and before
      ``pytest``:

        - name: Alembic head-multiplicity gate (ADR-0045)
          working-directory: services/api
          run: |
            heads=$(uv run alembic heads)
            count=$(echo "$heads" | wc -l)
            if [ "$count" -ne 1 ]; then
              echo "::error::Multiple alembic heads detected:"
              echo "$heads"
              exit 1
            fi
            echo "Single head: $heads"

      Idempotent: re-runs see single head and pass. Failure path
      surfaces all conflicting heads so the author sees which
      revision IDs collide.

      Tests: the step's success path is covered by the workflow
      itself (the migration graph in main is always single-head, so
      every CI run is a passing test). The failure path is covered
      by the smoke test in the next task.
    scope:
      files:
        - .github/workflows/ci.yml
    validation:
      - kind: deterministic
        description: |
          CI step text present + grep matches expected lines.
        script: |
          grep -q "Alembic head-multiplicity gate" .github/workflows/ci.yml \
            && grep -q "alembic heads" .github/workflows/ci.yml

  - id: smoke-colliding-revision-fails-ci
    title: Smoke — colliding revision PR fails the new CI gate
    workflow: wf-validate
    depends_on:
      - task.ci-gate-alembic-heads.pr_merged
    intent: |
      Open a deliberately-failing smoke PR: add a new migration
      file at ``services/api/alembic/versions/20260517_0000_colliding_smoke.py``
      claiming ``revision="0017"`` (which already exists). Wait for
      CI; observe the new step fails with an error listing both 0017
      heads. Close the PR without merging.

      Document in ``docs/handoffs/2026-05-17-alembic-heads-gate-smoke.md``:
      the smoke PR number, the CI run URL, the exact error output, and
      confirmation that branch protection now requires this check.
    scope:
      files:
        - docs/handoffs/2026-05-17-alembic-heads-gate-smoke.md
    validation:
      - kind: deterministic
        description: |
          Handoff names the smoke PR + the failure observed.
        script: |
          test -f docs/handoffs/2026-05-17-alembic-heads-gate-smoke.md \
            && grep -qi "alembic.*head\|head.*alembic" docs/handoffs/2026-05-17-alembic-heads-gate-smoke.md \
            && grep -qi "ci.*fail\|fail.*ci\|smoke PR" docs/handoffs/2026-05-17-alembic-heads-gate-smoke.md
```

## Decisions captured during execution

(empty)

## Post-mortem

Filled in on transition to `completed` / `abandoned`.
