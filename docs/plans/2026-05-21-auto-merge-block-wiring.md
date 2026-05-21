---
auto_merge: false
status: active
---

# Plan: Wire the repo-level auto-merge block into the live trigger (ADR-0050 wave 3)

- **Status:** active
- **Date:** 2026-05-21
- **Related ADRs:** ADR-0050 (decision 5 — repo config blocks auto-merge), ADR-0031 (auto-merge cooling-off), ADR-0030 (docs-current-with-pr)
- **Related plans:** 2026-05-21-onboarding-persistence (the store this reads)

## Goal

Make the per-repo `auto_merge_blocked` config actually stop auto-merge. Add a
fail-open check, backed by the merged `OnboardingStore`, to both auto-merge
gates: the cooling-off setter and the fire-time re-check. This is ADR-0050
decision 5's payoff — the repo config finally *does* something.

**Manual-merge (`auto_merge: false`):** this touches the live auto-merge path,
so the operator reviews the diff before it lands. The change is low-risk by
construction — it fails open (any error or missing config → not blocked) and is
a no-op for any repo without a `repo_configs` row (including Treadmill itself).

## Success criteria

- A repo whose `repo_configs.auto_merge_blocked = true` never has its tasks'
  PRs auto-merged; both the cooling-off setter and the fire-time gate skip.
- Repos with no config row (today: all of them) behave exactly as before.
- A config-lookup failure fails **open** (auto-merge proceeds), never blocks
  spuriously.

## Constraints / scope

### In scope
The single task below: a `_repo_auto_merge_blocked` helper + its two call sites
in `coordination/triggers.py`, a unit test, and the AGENT.md update.

### Out of scope
The onboarding API endpoints, `wf-discover` + `role-cartographer`, the
context-provider wiring, adapt-mode validation, the CDK S3 bucket. Later waves.

### Budget
One task. If the helper can't be made fail-open and unit-tested without a DB,
it fails the deterministic check rather than merging.

## sequence_of_work

```yaml
sequence_of_work:
  - id: auto-merge-block-wiring
    title: Repo-level auto-merge block in the live trigger (ADR-0050 d.5)
    workflow: wf-author
    intent: |
      Wire ADR-0050's per-repo ``auto_merge_blocked`` config into the live
      auto-merge trigger. Build on the MERGED ``OnboardingStore`` in
      ``treadmill_api/onboarding_store.py`` (``async get_repo_config(session,
      repo) -> RepoConfig | None``; ``RepoConfig.auto_merge_blocked: bool``).
      Read ``treadmill_api/coordination/triggers.py`` first.

      (1) Add a module-level async helper in
      ``treadmill_api/coordination/triggers.py``:

        async def _repo_auto_merge_blocked(session, repo: str) -> bool:
            '''True iff the repo's onboarding config blocks all auto-merge
            (ADR-0050 d.5). Fail-OPEN: missing config or any error -> False,
            preserving pre-ADR-0050 behavior for repos without a config row.'''

      Implement it to: return False for a falsy ``repo``; otherwise call
      ``OnboardingStore().get_repo_config(session, repo)`` inside a
      ``try/except Exception`` that logs a warning and returns False on error;
      return ``bool(config and config.auto_merge_blocked)``. Import
      ``OnboardingStore`` from ``treadmill_api.onboarding_store`` at the top of
      the module (no circular import — onboarding_store does not import
      triggers).

      (2) Call it in ``maybe_auto_merge_on_mergeable``: immediately AFTER the
      existing ``if merge_row.auto_merge is False:`` skip block (which already
      has ``merge_row.repo`` available), add:

            if await _repo_auto_merge_blocked(session, merge_row.repo):
                logger.info(
                    "auto-merge: repo %s auto_merge_blocked (ADR-0050); "
                    "skipping task %s", merge_row.repo, task_id,
                )
                return False

      (3) Call it in ``_check_still_mergeable_for_auto_merge`` (the fire-time
      gate): add ``tm.repo`` to that function's SELECT column list, then after
      the existing ``if row.auto_merge is False: return False`` add:

            if await _repo_auto_merge_blocked(session, row.repo):
                return False

      Do not change any other skip logic or the cooling-off timing.

      (4) NEW test file ``services/api/tests/test_auto_merge_block.py`` — unit
      tests for the helper only (no DB; patch the store). Use
      ``unittest.mock`` to patch
      ``treadmill_api.coordination.triggers.OnboardingStore`` so its
      ``get_repo_config`` is an ``AsyncMock``, and assert (with
      ``pytest.mark.asyncio``; a dummy session object is fine):
        - config with ``auto_merge_blocked=True`` -> helper returns True;
        - config with ``auto_merge_blocked=False`` -> returns False;
        - ``get_repo_config`` returns None -> returns False;
        - ``get_repo_config`` raises -> returns False (fail-open);
        - empty ``repo`` ("") -> returns False without calling the store.

      (5) DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update
      ``services/api/AGENT.md`` — note that the auto-merge trigger now honors a
      per-repo ``auto_merge_blocked`` config (ADR-0050) in addition to the
      plan-level flag, in "Key surfaces"/"Recent changes" as fits.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_auto_merge_block.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/onboarding_store.py
        - services/api/treadmill_api/models/onboarding.py
    validation:
      - kind: deterministic
        description: |
          The helper exists, is called at both gates, and the fail-open unit
          tests pass.
        script: |
          cd services/api \
            && grep -q "async def _repo_auto_merge_blocked" treadmill_api/coordination/triggers.py \
            && [ "$(grep -c 'await _repo_auto_merge_blocked' treadmill_api/coordination/triggers.py)" = "2" ] \
            && uv run pytest tests/test_auto_merge_block.py -q
```

## Risks / unknowns

- **Live auto-merge path:** mitigated by fail-open + no-op-without-config +
  manual review before merge. CI runs the full api suite (regression guard).
- **Two gates:** the setter prevents the deadline; the fire-time gate is the
  last line before the merge call. Both check, so a config added mid-cooldown
  still blocks.

## Decisions captured during execution

- **Fail-open** on lookup error/missing config — a repo-config bug must never
  silently freeze auto-merge for repos that never opted in.

## Post-mortem

_(filled when the wave completes)_
