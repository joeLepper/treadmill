---
auto_merge: true
status: active
---

# Plan: Deploy-watcher pulls local before building (close the autodeploy story)

- **Status:** active
- **Date:** 2026-05-26
- **Related ADRs:** ADR-0024 (local-mode auto-redeploy-on-merge)
- **Related learnings:** `docs/learnings/2026-05-22-docker-restart-reuses-old-image-silent-noop-deploy.md`

## Goal

Close the autodeploy silent-no-op story. The 2026-05-22 fix made
`_action_api`/`_action_agent` **recreate** containers from the freshly-built
image (was `docker restart` reusing the old image). That works — verified
2026-05-26 via the OTel canary (`treadmill-api` correctly cycled to a new
image in 14 s post-merge). But the sibling gap remained: the watcher does
`docker build <local_dir>` **without first pulling the local clone**, so when
`/home/joe/treadmill` is behind `origin/main` the "fresh" image is built from
**stale pre-merge source** — silently no-op'ing the deploy. The canary's OTel
fix shipped to `origin/main` but the rebuilt API image still contained the
gRPC exporter + `insecure=True`; the flood persisted until the operator
manually ff'd the local repo + rebuilt. This wave adds a `git fetch` +
`merge --ff-only origin/main` to the watcher before each `docker build`, so
auto-deploys actually deploy.

## Success criteria

- Before each `docker build` in `_action_api` and `_action_agent`, the watcher
  fetches `origin` + tries `git merge --ff-only origin/main` in the local repo.
- If the fast-forward succeeds, the build sees current source.
- If it fails (operator has unpushed/diverged commits), the watcher **logs a
  clear warning** and falls back to building from the current local state
  (preserves today's behavior — no new silent-skip vector).
- A focused test asserts the fetch+merge call happens before `docker build` in
  both actions, and that ff-failure does not break the deploy path.

## Constraints / scope

### In scope
`tools/local-adapter/treadmill_local/deploy_watcher.py`
(`_action_api`, `_action_agent`, a new shared helper), tests, and the
component AGENT.md update.

### Out of scope
Switching to the exact `merge_sha` from the pr_merged event (a follow-up;
ff-to-origin/main is enough for v1 since the watcher already serializes
actions). Pulling for `infra/`/`observability/` build categories (handled by
the same helper if any of those also `docker build` from local; otherwise
no-op). Changes to the recreate path itself.

### Budget
One task, `auto_merge: true`. Touches a single host-side file in
`tools/local-adapter/`. After merge it needs the deploy-watcher subprocess
restarted to pick up the change (operator: kill pid + `treadmill-local up
--no-build`).

## sequence_of_work

```yaml
sequence_of_work:
  - id: deploy-watcher-pull-before-build
    title: Deploy-watcher pulls local before docker build (close ADR-0024)
    workflow: wf-author
    intent: |
      Add a ``_sync_local_to_origin()`` helper to ``DeployWatcher`` and call
      it at the top of BOTH ``_action_api`` and ``_action_agent``, BEFORE the
      existing ``docker build`` subprocess call. Read first:
      ``tools/local-adapter/treadmill_local/deploy_watcher.py`` — find
      ``_action_api`` (the recreate path that calls ``self._recreate_api_fn``)
      and ``_action_agent`` (``docker build -t treadmill-agent:dev ...``). The
      watcher already holds ``self.repo_root`` (the local clone root).

      (1) HELPER — ``def _sync_local_to_origin(self) -> None:`` on
      ``DeployWatcher``. Steps:
        - ``subprocess.run(["git","-C",str(self.repo_root),"fetch","origin","--quiet"], check=False)``.
          On non-zero exit, log a warning and return (don't break the deploy).
        - ``subprocess.run(["git","-C",str(self.repo_root),"merge","--ff-only","origin/main"], check=False, capture_output=True, text=True)``.
          On success (returncode 0): log INFO with the new HEAD short sha
          (``git -C <root> rev-parse --short HEAD``).
          On non-zero (divergence — operator has unpushed work, or local has a
          different branch checked out): log a WARNING that includes the
          stderr; **return without raising** — fall back to building from
          current local state (preserves today's behavior; no new silent-skip
          vector for normal merges).
        - Pure subprocess calls (no GitPython dep). All non-`fetch`/`merge`
          subprocess shape mirrors the existing ``docker build`` call style.

      (2) CALL SITES — at the very top of ``_action_api()`` and
      ``_action_agent()``, before any ``docker build`` call:
      ``self._sync_local_to_origin()``. Keep all other logic unchanged.

      (3) TESTS — extend ``tools/local-adapter/tests/test_deploy_watcher.py``:
        * Patch ``subprocess.run`` (the watcher's existing patch pattern).
          Drive ``_action_api`` via ``_process_message`` (the existing
          test_api_action_builds_and_recreates pattern). Assert the FIRST two
          subprocess calls are ``git ... fetch origin --quiet`` and
          ``git ... merge --ff-only origin/main``, then the existing
          ``docker build`` call. Same for ``_action_agent``.
        * A ``test_api_action_continues_when_ff_fails``: make the merge
          subprocess return a non-zero returncode + stderr text; assert
          ``docker build`` still runs (no exception raised) and a warning
          log was emitted (use ``caplog`` to capture).
        * Keep all existing deploy-watcher tests green.

      (4) DOCS (ADR-0030 — REQUIRED): update
      ``tools/local-adapter/AGENT.md`` — note ``_action_api``/``_action_agent``
      now ``git fetch + merge --ff-only origin/main`` before each
      ``docker build`` so the image is built from current source (closes the
      ADR-0024 stale-source sibling to the recreate fix); ff-only failure
      warns + falls back to local state.
    scope:
      files:
        - tools/local-adapter/treadmill_local/deploy_watcher.py
        - tools/local-adapter/tests/test_deploy_watcher.py
        - tools/local-adapter/AGENT.md
      out_of_scope:
        - services/api/
        - workers/agent/
    validation:
      - kind: deterministic
        description: |
          The new helper exists, both action methods call it, and deploy-watcher
          tests pass.
        script: |
          grep -q "_sync_local_to_origin" tools/local-adapter/treadmill_local/deploy_watcher.py \
            && grep -q "merge.*--ff-only" tools/local-adapter/treadmill_local/deploy_watcher.py \
            && cd tools/local-adapter && uv run pytest tests/test_deploy_watcher.py -q
```

## Risks / unknowns

- **Operator with uncommitted/unpushed local work:** ff-only fails → warning +
  build proceeds from current local. Acceptable (preserves today's behavior;
  the operator sees the warning).
- **Concurrent merges racing the fetch:** the watcher serializes deploy events
  (one at a time per SQS message); a brand-new merge might land between
  fetch and build, but the next deploy event for that merge will be processed
  in turn. Acceptable.
- **Post-merge deploy:** the deploy-watcher subprocess must be restarted
  (operator: kill pid + `treadmill-local up --no-build`) for the change to
  take effect — that's the FIRST deploy that proves itself end-to-end.

## Post-mortem

_(filled when the wave completes)_
