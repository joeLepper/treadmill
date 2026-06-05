# Autoscaler ignores `--no-build` and stalls silently on image build failures

**Date:** 2026-06-05
**Related:** ADR-0018 (autoscaler), `project_deploy_watcher_builds_from_stale_source`, ADR-0075

## What happened

A TypeScript bug landed on `main` in PR #179 (`dspy_variant_pr.tsx` declared
`submitDisabled` in one component and referenced it from another, out of scope).
The dev-local autoscaler — which calls `_ensure_images_built()` on every worker
spawn — then crashed every tick with:

> RuntimeError: docker build failed for treadmill-dashboard:dev; refusing to
> start containers with a stale image. Re-run with `--no-build` to bypass.

The autoscaler kept ticking and logging the same error every 5 seconds for
~1h26m (400+ consecutive failed ticks) before an operator noticed. Zero workers
ever spawned; the SQS queue grew; in-flight labelling tasks (substep 2.2, 3.2,
plus the just-dispatched ADR-0074 pair) sat in `pending` with no progress.

The operator misread the silence as "the loop is chewing tokens" and asked
to investigate — the truth was the inverse: nothing was running.

## Two structural gaps it exposes

1. **`--no-build` is not honored by the autoscaler.** `treadmill-local up
   --no-build` sets `LocalRuntime(build_images=False)` in the parent process,
   but the autoscaler subprocess constructs its own `LocalRuntime` in
   `autoscaler.py:378` without that flag. `start_worker_once` then calls
   `_ensure_images_built()` unconditionally. The operator's documented escape
   hatch ("re-run with --no-build") only fixes one side.

2. **Image-build wedges are invisible to the operator.** The autoscaler's only
   output is `.treadmill-local/autoscaler.log`, which an operator only checks
   when something looks wrong. There is no surface that says "the queue isn't
   draining because workers aren't starting." `treadmill task list` shows
   tasks as `wf-author: executing` even when no step has a `started_at` —
   `derived_status` reads from workflow_run_steps existence, not from progress.

The "indicator says executing but the step is `pending` with no `started_at`"
gap is the more dangerous of the two — operators can't see queue starvation
from the surface they normally watch.

## Sibling pattern

This is the same shape as `project_deploy_watcher_builds_from_stale_source`:
a background loop that holds the system's freshness invariant, but fails
silently (just-keeps-trying) instead of escalating. There the symptom was
"the container ran but from stale code"; here it's "no container ran at all."
Both eat hours before a human notices.

## What we did right now

- Fixed the TS bug (move `submitDisabled` into the `LabelColumn` scope where
  its inputs live; remove the stale declaration).
- Restarted the autoscaler — once the dashboard image built clean, workers
  started spawning within one tick (5s) and the 11-message backlog drained.

## What should change durably

- **Plumb `--no-build` through to the autoscaler.** Either an env var
  (`TREADMILL_AUTOSCALER_BUILD_IMAGES=false`) read in `autoscaler.py:378`,
  or pass it via the launcher in `runtime.py:_start_autoscaler_dev_local`.
  This is the small, mechanical fix that closes the "documented escape hatch
  doesn't actually escape" loop.

- **Make the wedge loud.** Two options worth weighing:
  - On N consecutive failed ticks (say N=12, ≈1 min), have the autoscaler
    write a `system_event` of kind `autoscaler_wedged` that the dashboard
    surfaces, and post to the operator-relay (ADR-0071 significant set —
    this is an unexpected terminal state for the worker fleet).
  - Reflect "no worker spawn for N seconds despite visible>0" on
    `treadmill task list` so a task in `wf-author: executing` whose
    workflow_run_steps row has been `pending` for >5 min reads as
    `stalled (no worker)` instead of `executing`.

  The first is easier; the second is more correct (it's the gap between
  *step exists* and *step is being worked*, which is broader than just
  image-build wedges — same gap will surface any future starvation cause).

- **CI should fail dashboard builds on TS errors.** PR #179 merged green even
  though the dashboard image cannot build from the merged tree. The CI gate
  for dashboard PRs should run the same `tsc -b && vite build` the Dockerfile
  runs, not a stricter or weaker subset. Otherwise main can — and did — land
  in a state where dev-local boots but image rebuilds are wedged.

## How an operator catches this faster next time

If "tasks look stuck" — check workflow_run_steps first, not just task status:

```sql
select step_name, role_id, status, started_at, completed_at
from workflow_run_steps wrs
join workflow_runs wr on wr.id = wrs.run_id
where wr.task_id = '<uuid>'
order by wrs.id desc limit 10;
```

If `status='pending'` and `started_at is null` for >5 min, no worker has
picked it up — check `.treadmill-local/autoscaler.log` for tick errors.
