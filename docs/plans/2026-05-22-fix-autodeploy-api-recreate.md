---
auto_merge: true
status: active
---

# Plan: Fix the deploy-watcher's API auto-deploy (recreate, not restart)

- **Status:** active
- **Date:** 2026-05-22
- **Related ADRs:** ADR-0024 (local-mode auto-redeploy on merge)
- **Related learnings:** 2026-05-22-docker-restart-reuses-old-image-silent-noop-deploy

## Goal

The deploy-watcher's `_action_api` does `docker build` + `docker restart` — but
`docker restart` reuses the existing container's image, so the freshly-built
image never runs. Every `services/api/**` autodeploy has been a **silent no-op**
(new code/migrations built, never run); API changes only went live via manual
`up`/`redeploy`. Fix it so api-change merges actually deploy: recreate the API
container from the new image, and fix the wrong health-check port.

## Success criteria

- After an `services/api/**` merge, the deploy-watcher leaves the **new image
  running** (the live `treadmill-api` container's image id matches the freshly
  built `treadmill-api:dev`), so the container-startup `alembic upgrade` and new
  code take effect — no manual `redeploy` needed.
- The post-deploy health check passes against the **actual** dev-local API port.
- Verified by the operator via a real deploy cycle (the fix can't auto-deploy
  itself — chicken-and-egg — so the operator deploys it once by hand, then
  confirms a *subsequent* api merge auto-deploys).

## Constraints / scope

### In scope
`_action_api` (recreate instead of restart) + the health-check port, in
`tools/local-adapter/treadmill_local/deploy_watcher.py` (and a runtime helper if
recreate needs the API container's run config), tests, and the AGENT.md.

### Out of scope
`_action_agent` (build-only is correct — workers are one-shot); the scheduler /
autoscaler / logging code; changing the deploy-events plumbing.

### Budget
One task. `auto_merge: true` — CI gates the code; the deploy-behavior proof is an
operator deploy-cycle *after* merge regardless (and the broken deploy-watcher
can't auto-deploy its own fix), so manual-merge buys no pre-merge verification.

## sequence_of_work

```yaml
sequence_of_work:
  - id: autodeploy-api-recreate
    title: Deploy-watcher recreates the API container from the new image (ADR-0024)
    workflow: wf-author
    intent: |
      Fix the deploy-watcher's silent-no-op API auto-deploy. Read first:
      `tools/local-adapter/treadmill_local/deploy_watcher.py` `_action_api`
      (it does `docker build -t treadmill-api:dev <api_dir>` then
      `docker restart treadmill-api` + `_wait_healthy("http://localhost:8000/health/ready")`).
      The bug: `docker restart` re-runs the EXISTING container's image — the
      freshly-built image never runs, so api code + migrations never go live.
      Also `:8000` is wrong for dev-local (it serves `:8088`).

      (1) RECREATE, don't restart. After the `docker build`, recreate the
      `treadmill-api` container FROM the new image with the SAME run config
      (env, ports, network, name) the container was originally created with — do
      NOT `docker restart`, and do NOT blindly `docker run` without the real
      config. The correct run config lives in
      `tools/local-adapter/treadmill_local/runtime.py` (how `up` / the
      dev-local service start creates the API container). Reuse that path:
      prefer importing/calling a runtime helper that (re)creates the API
      container (add a small `recreate_api_container`-style helper to
      `runtime.py` if one doesn't exist, factored from the existing
      `_start_services` / API-spec creation, so the deploy-watcher and `up`
      share one creation path). The watcher must end with the NEW image
      actually running.

      (2) HEALTH PORT. Fix `_wait_healthy` to target the dev-local API port from
      the deployment config (the watcher already loads the deployment id; the
      port is in `~/.treadmill/<id>.yaml` / the runtime's config), not a
      hardcoded `:8000`. Read how other code resolves the dev-local API URL and
      reuse it.

      (3) TESTS — `tools/local-adapter/tests/` (extend the deploy-watcher tests
      or add a focused one): patch `subprocess`/the runtime so no real docker
      runs, and assert `_action_api` (a) builds the image AND (b) recreates the
      container from it (stop+rm+run or the runtime recreate helper) rather than
      calling `docker restart`; and that the health URL uses the configured port,
      not `:8000`. Keep existing deploy-watcher tests green.

      (4) DOCS (ADR-0030 — REQUIRED): update `tools/local-adapter/AGENT.md` —
      note `_action_api` now recreates the container from the new image (was a
      silent no-op via `docker restart`) and health-checks the configured port.
    scope:
      files:
        - tools/local-adapter/treadmill_local/deploy_watcher.py
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/tests/test_deploy_watcher.py
        - tools/local-adapter/AGENT.md
      out_of_scope:
        - tools/local-adapter/treadmill_local/autoscaler.py
        - tools/local-adapter/treadmill_local/subprocess_logging.py
    validation:
      - kind: deterministic
        description: |
          The api action no longer uses `docker restart` (recreates instead) and
          the deploy-watcher tests pass.
        script: |
          cd tools/local-adapter \
            && ! grep -q 'docker", "restart", "treadmill-api' treadmill_local/deploy_watcher.py \
            && uv run pytest tests/ -q -k "deploy_watcher or action_api"
```

## Risks / unknowns

- **Recreate must match the original run config** (env/ports/network) — blindly
  `docker run` would start a misconfigured API. Reusing the runtime's creation
  path is the guard; the operator's deploy-cycle smoke confirms.
- **High-blast (deploy path):** a bad recreate could leave the API down. Mitigation:
  the operator deploys the fix by hand first (recreating the deploy-watcher +
  API) and watches it come up healthy before relying on it.
- **Concurrent session in `tools/local-adapter`:** resolve any conflict at merge
  (auto-merge will hold if DIRTY).

## Decisions captured during execution

- **Recreate, not restart** — `docker restart` reuses the old image; the
  deploy-watcher and `up` should share one container-creation path so a deploy
  actually runs the new image.

## Post-mortem

_(filled when the wave completes)_
