---
auto_merge: true
status: active
---

# Plan: Bound the scheduler subprocess log (the deferred sibling)

- **Status:** active
- **Date:** 2026-05-22
- **Related ADRs:** ADR-0035 (scheduler), ADR-0030 (docs-current-with-pr)
- **Related plans:** 2026-05-21-bound-subprocess-logs (autoscaler + deploy-watcher; this completes the population)

## Goal

Apply the same bounded-logging fix shipped for the autoscaler + deploy-watcher to
the **scheduler** subprocess (`treadmill_api.scheduler.runner`), the last
unbounded-logging subprocess `treadmill-local up` spawns. Its `scheduler.log`
held 463 identical `ConnectionRefusedError` tracebacks during the recent
dependency outage — same pattern (no rotation + a full traceback per loop
iteration). This closes the systematic audit.

## Success criteria

- The scheduler's log is **size-bounded** (rotated: cap + a few backups); a
  persistent dependency outage produces a bounded log, not an unbounded one.
- A persistent loop error logs its traceback **once per signature**, then
  rate-limited counted summaries; recovery resets it.
- The parent spawn no longer redirects the scheduler's output to a raw append
  file — the subprocess owns its rotating log; existing scheduler behavior +
  tests stay green.

## Constraints / scope

### In scope
The scheduler subprocess's logging (in `services/api`) + its spawn site in
`tools/local-adapter/runtime.py`, plus tests and both touched components'
AGENT.md.

### Out of scope
Changing scheduler *behavior* (poll cadence, dispatch, replay). Importing the
local-adapter's `subprocess_logging` helper into `services/api` — the two are
separate packages; mirror the shape instead.

### Budget
One task. Concurrent second session is active in `services/api`/`runtime.py`;
`auto_merge: true` per the relaxed coordination rule — resolve any AGENT.md /
runtime.py conflict at merge (as we did for #239).

## sequence_of_work

```yaml
sequence_of_work:
  - id: bound-scheduler-log
    title: Rotate + rate-limit the scheduler subprocess log (ADR-0035)
    workflow: wf-author
    intent: |
      Bound the scheduler subprocess log, mirroring the shipped autoscaler +
      deploy-watcher fix. The scheduler runs as ``python -m
      treadmill_api.scheduler.runner`` (spawned by the local-adapter); its log
      currently grows without bound and dumps a full traceback per loop
      iteration on a persistent error. Read first:
      ``services/api/treadmill_api/scheduler/runner.py`` (find its
      ``main``/``__main__`` logging setup — likely ``logging.basicConfig`` — and
      its poll loop's ``except`` that logs errors), and the SCHEDULER spawn site
      in ``tools/local-adapter/treadmill_local/runtime.py`` (search
      ``SCHEDULER_LOG_FILE`` — it does ``open(SCHEDULER_LOG_FILE, "ab")`` then
      ``Popen(stdout=log_handle, stderr=subprocess.STDOUT)``). For reference, the
      already-merged equivalent is
      ``tools/local-adapter/treadmill_local/subprocess_logging.py``
      (``configure_rotating_logging`` + ``RateLimitedErrorLogger``) — mirror its
      shape, but DO NOT import it (separate package).

      (1) NEW module ``services/api/treadmill_api/scheduler/bounded_logging.py``
      (treadmill_api package — no dependency on treadmill_local):
        - ``configure_rotating_logging(log_file: Path, *, level=logging.INFO,
          max_bytes=10_000_000, backups=3) -> None`` — configure the
          ``treadmill`` (or root) logger with a
          ``logging.handlers.RotatingFileHandler`` + a formatter, REPLACING the
          default stdout handler (so nothing goes to stdout → the parent's
          redirect captures nothing). Make the parent dir if needed.
        - A ``RateLimitedErrorLogger`` (or function) that logs the FULL traceback
          on the first occurrence of an error signature (``type(exc).__name__`` +
          first line of ``str(exc)``), then a counted one-line summary at most
          once per ``summary_every`` (default 50) repeats; ``reset()`` (on a
          successful iteration) re-arms a fresh traceback.

      (2) ``scheduler/runner.py``: in the entrypoint, replace the
      ``logging.basicConfig`` stdout setup with ``configure_rotating_logging``
      writing to the log path from env ``TREADMILL_SCHEDULER_LOG_FILE`` (fall
      back to a sensible default if unset). In the poll loop's error handler,
      replace the per-iteration ``logger.exception(...)`` with the rate-limited
      logger; call ``reset()`` after a successful poll/tick.

      (3) ``tools/local-adapter/treadmill_local/runtime.py``: at the scheduler
      spawn site, add ``TREADMILL_SCHEDULER_LOG_FILE`` (the existing
      ``SCHEDULER_LOG_FILE`` constant) to the subprocess ``env``, and change the
      Popen redirect from the raw ``log_handle`` to ``stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL``; remove the now-unused
      ``log_handle = open(SCHEDULER_LOG_FILE, "ab")`` for the scheduler. Leave
      the autoscaler + deploy-watcher spawns (already fixed) untouched.

      (4) TESTS — ``services/api/tests/test_scheduler_bounded_logging.py`` (new):
        - rotation: ``configure_rotating_logging`` with a tiny ``max_bytes`` to a
          tmp file; emit enough records to exceed it; assert a backup appears and
          the main file stays ≤ cap.
        - rate-limit: feed the helper the same exception type 200×; assert ONE
          full traceback + bounded summaries (far fewer than 200), and ``reset()``
          re-arms. Keep existing scheduler/runner tests green; scope in the
          existing scheduler test file if the entrypoint change touches it.

      (5) DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update BOTH
      ``services/api/AGENT.md`` (new ``scheduler/bounded_logging.py`` + the
      runner now rotates/rate-limits) and ``tools/local-adapter/AGENT.md`` (the
      scheduler spawn now redirects to DEVNULL + passes the log path), each with
      a Key surfaces / Recent changes entry.
    scope:
      files:
        - services/api/treadmill_api/scheduler/bounded_logging.py
        - services/api/treadmill_api/scheduler/runner.py
        - services/api/tests/test_scheduler_bounded_logging.py
        - services/api/AGENT.md
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/AGENT.md
      out_of_scope:
        - tools/local-adapter/treadmill_local/subprocess_logging.py
        - tools/local-adapter/treadmill_local/autoscaler.py
        - tools/local-adapter/treadmill_local/deploy_watcher.py
    validation:
      - kind: deterministic
        description: |
          The scheduler rotating/rate-limited logging helper exists and its
          tests pass.
        script: |
          cd services/api \
            && grep -q "RotatingFileHandler" treadmill_api/scheduler/bounded_logging.py \
            && uv run pytest tests/test_scheduler_bounded_logging.py -q
```

## Risks / unknowns

- **Concurrent session on `services/api` + `runtime.py`:** scopes specific files
  (`scheduler/runner.py`, a new module) unlikely to overlap onboarding work;
  `runtime.py` change is the scheduler spawn block only. Resolve any conflict at
  merge.
- **DEVNULL drops a rare process-killing traceback** — acceptable; the storm
  (caught loop errors) now goes through the rotating + rate-limited path.

## Decisions captured during execution

- **Mirror, don't share** — the rotating/rate-limit helper is duplicated into
  `services/api` rather than imported from `treadmill_local` (separate packages).

## Post-mortem

_(filled when the wave completes)_
