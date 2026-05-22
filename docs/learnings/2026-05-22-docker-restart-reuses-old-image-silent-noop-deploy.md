---
date: 2026-05-22
trigger: incident
status: captured
related: ADR-0024, 2026-05-22-fix-autodeploy-api-recreate, 2026-05-22-make-scheduler-load-bearing
---

# Learning: `docker restart` reuses the old image — the deploy-watcher's API auto-deploy was a silent no-op

## Trigger

P1 (`workflow_runs.task_id` nullable migration) merged, the deploy-watcher
"processed" the PR, and the API container even restarted — yet the dev DB stayed
at the old alembic revision. The migration only applied when I ran
`alembic upgrade` by hand.

## Observation

The API container *does* self-migrate at startup
(`treadmill_api.cli` runs `alembic upgrade head`, fail-fast, replica-safe). So
the migration mechanism was fine. The break was in the deploy-watcher's
`_action_api` (`tools/local-adapter/treadmill_local/deploy_watcher.py`):

```python
subprocess.run(["docker", "build", "-t", "treadmill-api:dev", api_dir], check=True)
subprocess.run(["docker", "restart", "treadmill-api"], check=True)   # bug
```

**`docker restart` restarts the existing container with its existing image — it
does not run a freshly-built image.** So every `services/api/**` autodeploy built
a new `treadmill-api:dev` that never ran; the live container kept the old code +
old alembic head, making the startup `alembic upgrade` a no-op. The action even
returned exit 0 ("build ok, restart ok") — a textbook **silent failure**: the
commands succeeded, the new code never ran. (Secondary: `_wait_healthy` hardcodes
`http://localhost:8000/health/ready`, but dev-local serves `:8088` — so the
action also errored on the health check and re-churned.)

Net: the deploy-watcher's API auto-deploy has been **silently ineffective** since
it was written. API changes only ever went live via a manual
`treadmill-local up`/`redeploy`, which *recreates* the container.

## Lesson

- **`docker restart` ≠ recreate-with-new-image.** To run a freshly-built image
  you must recreate the container (stop + rm + run with the same config), or
  reuse the runtime's container-creation path. `docker restart` only re-runs the
  same image.
- **A deploy "succeeding" (build ok + restart ok, exit 0) is not proof the new
  code is running.** Verify the deployed artifact is live (new revision / running
  image), not just that the deploy commands returned 0 — otherwise it's a silent
  no-op of exactly the load-bearing kind the reliability push targets.
- **Health checks must target the config-driven port**, not a hardcoded one
  (`:8000` vs dev-local's `:8088`).

## Toward a rule

Candidate: a deploy/reconcile action must assert the new image is *running*
(e.g. the container's image id matches the freshly-built one, or a version
endpoint reports the new sha) before declaring success. Fix:
`docs/plans/2026-05-22-fix-autodeploy-api-recreate.md`.
