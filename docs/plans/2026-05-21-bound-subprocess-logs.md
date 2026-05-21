---
auto_merge: true
status: active
---

# Plan: Bound subprocess logs + stop per-iteration traceback spam (IMMEDIATE)

- **Status:** active
- **Date:** 2026-05-21
- **Related ADRs:** ADR-0035 (scheduler), ADR-0024 (deploy-watcher), ADR-0030 (docs-current-with-pr)

## Goal

**Production incident (2026-05-21): the dev-local host ran out of disk overnight
because the autoscaler + deploy-watcher subprocess logs grew without bound** —
each detached subprocess redirects stdout/stderr to a raw `open(.., "ab")` file
(no rotation), and the poll loops log a **full traceback every iteration** via
`logger.exception(...)` when a dependency is unreachable (e.g. the 463 identical
`ConnectionRefusedError ('127.0.0.1', …)` tracebacks currently in
`scheduler.log`). Fix both the symptom (unbounded files) and the cause
(per-iteration traceback dumps) so it can't recur.

## Success criteria

- The autoscaler + deploy-watcher subprocess logs are **size-bounded** (rotated:
  cap + a few backups); a process that errors every poll for hours produces a
  bounded log, not an unbounded one.
- A **persistent** error logs its traceback **once**, then a rate-limited,
  counted summary (e.g. "ConnectionRefusedError ×N, still failing") — not a full
  traceback per iteration; recovery is logged and resets the counter.
- Existing local-adapter behavior + tests stay green.

## Constraints / scope

### In scope
The two subprocesses that caused the incident — `treadmill_local.autoscaler`
and `treadmill_local.deploy_watcher` — plus a shared logging helper and the
`runtime.py` spawn sites for those two.

### Out of scope
The **scheduler** subprocess (`treadmill_api.scheduler.runner`) has the *same*
flaw but lives in `services/api`, which the concurrent ramjac-bootstrap
session is actively editing — it's a **coordinated follow-up** (same fix,
separate wave) to avoid stomping that session. Also out: changing what the
subprocesses *do* on error (backoff policy beyond rate-limited logging).

### Budget
One task. Manual disk cleanup already done by the operator; this prevents
recurrence.

## sequence_of_work

```yaml
sequence_of_work:
  - id: bound-subprocess-logs
    title: Rotate + rate-limit autoscaler & deploy-watcher subprocess logs
    workflow: wf-author
    intent: |
      Fix the dev-local disk-fill incident: the autoscaler + deploy-watcher
      subprocess logs grow without bound, and the poll loops dump a full
      traceback every iteration on a persistent error. Read first:
      ``tools/local-adapter/treadmill_local/autoscaler.py`` (main() ~240,
      logging.basicConfig ~262, the loop except ~211 `logger.exception`),
      ``tools/local-adapter/treadmill_local/deploy_watcher.py`` (main() ~264,
      basicConfig ~279, loop except ~116), and the spawn sites in
      ``tools/local-adapter/treadmill_local/runtime.py`` (autoscaler ~1542-1560
      and the restart path ~1612-1644; deploy-watcher ~1852-1879 — each does
      ``log_handle = open(<FILE>, "ab")`` then ``Popen(stdout=log_handle,
      stderr=subprocess.STDOUT)``). Do NOT touch the scheduler spawn (~1748-1766)
      or ``treadmill_api`` — that's a separate coordinated wave.

      (1) NEW module ``tools/local-adapter/treadmill_local/subprocess_logging.py``:
        - ``configure_rotating_logging(log_file: Path, *, level=logging.INFO,
          max_bytes=10_000_000, backups=3) -> None`` — configure the root logger
          (or the ``treadmill`` logger) with a
          ``logging.handlers.RotatingFileHandler(log_file, maxBytes=max_bytes,
          backupCount=backups)`` + a standard formatter, REPLACING the default
          stdout handler. Make the parent dir if needed.
        - A small ``RateLimitedErrorLogger`` (or function) that, given a logger
          and an exception, logs the FULL traceback on the first occurrence of a
          given error signature (``type(exc).__name__`` + first line of str),
          then for repeats of the same signature logs a one-line counted summary
          at most once every ``summary_every`` occurrences (default 50) — e.g.
          "ConnectionRefusedError still failing (N consecutive)". A ``reset()``
          (called on a successful iteration) clears the counter so the next
          failure logs a fresh traceback. No full traceback per repeat.

      (2) ``autoscaler.py``: in main(), replace the ``logging.basicConfig(...)``
      stdout setup with ``configure_rotating_logging`` writing to the log path
      from env ``TREADMILL_AUTOSCALER_LOG_FILE`` (fall back to the existing
      default path if unset). In the poll loop, replace
      ``logger.exception("tick failed; continuing")`` with the rate-limited
      error logger; call its ``reset()`` after a successful tick.

      (3) ``deploy_watcher.py``: same — ``configure_rotating_logging`` from env
      ``TREADMILL_DEPLOY_WATCHER_LOG_FILE``; replace
      ``logger.exception("poll iteration failed; continuing")`` with the
      rate-limited logger + ``reset()`` on a clean poll.

      (4) ``runtime.py``: at the autoscaler (start + restart) and deploy-watcher
      spawn sites, (a) add the corresponding ``TREADMILL_*_LOG_FILE`` to the
      subprocess ``env`` (the existing ``AUTOSCALER_LOG_FILE`` /
      ``DEPLOY_WATCHER_LOG_FILE`` constants), and (b) change the Popen redirect
      from the raw ``log_handle`` to ``stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL`` and remove the now-unused
      ``log_handle = open(...)`` for those sites — the subprocess now OWNS its
      rotating log file. Leave the scheduler spawn untouched.

      (5) TESTS — ``tools/local-adapter/tests/test_subprocess_logging.py`` (new):
        - rotation: ``configure_rotating_logging`` with a tiny ``max_bytes`` to a
          tmp file; emit enough log records to exceed it; assert a backup file
          appears AND the main file stays ≤ cap (rotation happened).
        - rate-limit: feed the helper the same exception type 200× with a logging
          capture (``caplog`` or a memory handler); assert exactly ONE full
          traceback (record with ``exc_info``) plus bounded summaries — far fewer
          than 200 — and that ``reset()`` then re-arms a fresh traceback.
        Keep existing local-adapter tests green; scope in
        ``tools/local-adapter/tests/test_runtime_dev_local.py`` if the spawn-env
        / redirect change breaks an assertion there (update it to match
        DEVNULL + the new env var).

      (6) DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update
      ``tools/local-adapter/AGENT.md`` — note `subprocess_logging.py` (Key
      surfaces) and that autoscaler/deploy-watcher logs now rotate + rate-limit
      errors (Recent changes).
    scope:
      files:
        - tools/local-adapter/treadmill_local/subprocess_logging.py
        - tools/local-adapter/treadmill_local/autoscaler.py
        - tools/local-adapter/treadmill_local/deploy_watcher.py
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/tests/test_subprocess_logging.py
        - tools/local-adapter/tests/test_runtime_dev_local.py
        - tools/local-adapter/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/scheduler/runner.py
        - tools/local-adapter/treadmill_local/cli.py
    validation:
      - kind: deterministic
        description: |
          The rotating + rate-limited logging helper exists and its tests pass.
        script: |
          cd tools/local-adapter \
            && grep -q "RotatingFileHandler" treadmill_local/subprocess_logging.py \
            && uv run pytest tests/test_subprocess_logging.py -q
```

## Risks / unknowns

- **Concurrent session on `runtime.py`/`cli.py`:** the ramjac session edits
  `tools/local-adapter`. This task scopes `cli.py` OUT and touches only the
  autoscaler/deploy-watcher spawn regions of `runtime.py` — low overlap. Watch
  the merge; resolve if it conflicts.
- **DEVNULL drops a process-killing traceback** (rare, one-shot) — acceptable:
  the storm we're fixing is the caught-and-continued kind, which the rotating
  handler + rate-limiter now own. Liveness is still observable via the pid file
  + log mtime.

## Decisions captured during execution

- **Subprocess owns its rotating log; the parent stops raw-redirecting to an
  append file** — rotation must live where the long-running process is, not as a
  rotate-on-start in the spawner (a single overnight run is the failure case).
- **Scheduler deferred** to a coordinated wave (it's in `services/api`, the
  other session's turf) — same flaw, same fix.

## Post-mortem

_(filled when the wave completes)_
