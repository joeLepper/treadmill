---
auto_merge: true
status: active
---

# Plan: Operator `task cancel` command (stop a task cleanly)

- **Status:** active
- **Date:** 2026-05-22
- **Related ADRs:** ADR-0046 (operator task retry — the sibling pattern), ADR-0010 (task lifecycle)

## Goal

Give operators a clean way to **cancel a task** — there's currently no way to
stop a runaway or abandoned task (the CLI has `task show/list/retry` only). The
`TaskCancelled` event already exists and the `task_status` VIEW already maps a
`task.cancelled` event to terminal status `'cancelled'`; the
`wf-stuck-task-sweep` SQL already excludes cancelled tasks. So the only missing
piece is the **operator surface** to emit the event: an API endpoint + a CLI
command, mirroring the existing `retry` path. Immediate use: cancel 6 abandoned
`joeLepper/treadmill` tasks (stalled since 5/19) the new stuck-task-sweep just
escalated.

## Success criteria

- `treadmill task cancel <task-id> --reason "<why>"` publishes a `task.cancelled`
  event; the task's `derived_status` becomes `cancelled` (terminal).
- Cancelling an already-terminal task (`pr_merged`/`cancelled`/`done`) returns a
  clear 409 (no duplicate event), mirroring retry's cap-409 shape.
- A cancelled task stops being re-escalated by `wf-stuck-task-sweep` (it's
  excluded) and stops being dispatched (terminal status gates the evaluator).

## Constraints / scope

### In scope
The operator surface only: `POST /api/v1/tasks/{task_id}/cancel` + the CLI
command + the api-client method + tests + docs. The enforcement (terminal
status, dispatch gating, sweep exclusion) ALREADY EXISTS — do not rebuild it.

### Out of scope
- Cancelling in-flight **pending steps** of an actively-running task (emit
  `step.cancelled` for them) — a possible follow-up; v1 just marks the task
  terminal via `task.cancelled`, which gates future dispatch. Note this in the
  endpoint docstring.
- Any change to the `task_status` VIEW or a migration (the VIEW already maps
  `task.cancelled` → `cancelled`).

### Budget
One task. `auto_merge: true` — additive endpoint + CLI command, low overlap with
the concurrent session's onboarding work. (Bonus: when this `services/api` change
merges, it doubles as the first live test of the now-fixed deploy-watcher
auto-deploy.)

## sequence_of_work

```yaml
sequence_of_work:
  - id: task-cancel-command
    title: Add operator task-cancel (API endpoint + CLI command), ADR-0046 sibling
    workflow: wf-author
    intent: |
      Add an operator-facing way to cancel a task by emitting the existing
      ``TaskCancelled`` event. The event + terminal-status enforcement ALREADY
      EXIST — you are only adding the operator surface, mirroring the ``retry``
      path. Read first:
        * ``services/api/treadmill_api/routers/tasks.py`` — the
          ``POST /api/v1/tasks/{task_id}/retry`` endpoint (request model
          ``TaskRetryRequest`` with ``reason``, 404 task-not-found, 409 cap), and
          ``create_task`` for HOW a router publishes an event (the event
          publisher dependency + constructing the ``Event`` + ``await
          publisher.publish(event, payload)``).
        * ``services/api/treadmill_api/events/task.py`` — ``TaskCancelled``
          (entity_type=task, action=cancelled, has ``reason: str | None``).
        * ``services/api/treadmill_api/coordination/triggers.py`` —
          ``_TERMINAL_TASK_STATUSES = {pr_merged, cancelled, done}`` (the terminal
          set; use it / the ``derived_status`` to reject an already-terminal task).
        * ``cli/treadmill_cli/cli.py`` ``task_retry`` command +
          ``cli/treadmill_cli/api_client.py`` ``retry_task`` — mirror both.

      (1) API — ``routers/tasks.py``: add
      ``POST /api/v1/tasks/{task_id}/cancel`` with a ``TaskCancelRequest`` body
      ``{reason: str (min_length=1, max_length=500)}``. Behavior: 404 if the task
      doesn't exist; 409 if the task's ``derived_status`` is already terminal
      (one of ``_TERMINAL_TASK_STATUSES``) — don't emit a duplicate event;
      otherwise construct + publish a ``TaskCancelled`` (with ``reason``) via the
      SAME publisher path ``create_task`` uses, and return the updated
      ``TaskResponse`` (re-read ``derived_status`` after publish, or return 200
      with a small ``{task_id, status: "cancelled"}`` body — match whatever shape
      is simplest and consistent with the router's other responses). Add a
      docstring noting v1 marks the task terminal (gating future dispatch) but
      does not cancel already-pending in-flight steps.

      (2) CLIENT — ``api_client.py``: add ``cancel_task(self, task_id: str,
      reason: str) -> dict`` mirroring ``retry_task`` (POST to
      ``/api/v1/tasks/{task_id}/cancel`` with ``{"reason": reason}``; raise
      ``ApiError`` on non-2xx as retry does).

      (3) CLI — ``cli.py``: add a ``task cancel`` command mirroring ``task
      retry``: ``task_id`` arg + required ``--reason/-r``; call
      ``client.cancel_task``; on ``ApiError`` 404 → "task not found", 409 →
      "task already terminal — nothing to cancel"; on success print
      ``cancelled: <task_id>``.

      (4) TESTS:
        * API (mirror the retry endpoint test location/style): cancelling a
          live task publishes a ``task.cancelled`` event (assert via the test's
          publisher spy / event capture) and the task reads back terminal;
          cancelling an already-terminal task → 409 and NO event; unknown id →
          404.
        * CLI (mirror the retry CLI test): ``task cancel`` invokes
          ``cancel_task`` with the id + reason; 409 path prints the terminal
          message and exits non-zero.

      (5) DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update
      ``services/api/AGENT.md`` (new cancel endpoint) and, if the CLI has its own
      AGENT.md / command reference, note the ``task cancel`` command there.
    scope:
      files:
        - services/api/treadmill_api/routers/tasks.py
        - cli/treadmill_cli/api_client.py
        - cli/treadmill_cli/cli.py
        - services/api/tests/
        - cli/tests/
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/alembic/versions/
    validation:
      - kind: deterministic
        description: |
          The cancel endpoint + CLI command exist and their tests pass.
        script: |
          cd services/api && uv run pytest tests/ -q -k "cancel" \
            && cd ../cli && uv run pytest tests/ -q -k "cancel"
```

## Risks / unknowns

- **Publisher wiring in the router:** match exactly how `create_task` obtains the
  event publisher (a FastAPI dependency) — don't invent a new one.
- **Response shape:** if re-reading `derived_status` post-publish is awkward
  (event-sourced view may lag within the request), returning a minimal
  `{task_id, status: "cancelled"}` 200 is acceptable — the contract is "the
  event was emitted."
- **Concurrent session:** scopes `routers/tasks.py` + `cli.py` (the sibling
  recently added a `docs` CLI group) — additive command, resolve any cli.py
  conflict at merge.

## Post-mortem

_(filled when the wave completes)_
