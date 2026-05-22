---
auto_merge: true
status: active
---

# Plan: Operator `task cancel` command (v2 — corrected publish wiring)

- **Status:** active
- **Date:** 2026-05-22
- **Related ADRs:** ADR-0046 (operator task retry — sibling pattern), ADR-0010 (task lifecycle)
- **Supersedes:** 2026-05-22-task-cancel-command (v1 — its worker looped: it
  published the event "via the dispatcher" with the WRONG method, so the
  author-side validation never passed and nothing pushed).

## Goal

Same as v1: an operator `task cancel` command (API endpoint + CLI) that emits the
existing `TaskCancelled` event (terminal-status enforcement + the `task_status`
VIEW's `cancelled` mapping + the sweep's exclusion already exist). v2 fixes the
two things that wedged v1: the **publish wiring** and the **validation script**.

## Success criteria

- `treadmill task cancel <task-id> --reason "<why>"` → the task's
  `derived_status` becomes `cancelled` (terminal); 409 if already terminal; 404
  if unknown.
- The author-side validation passes first try (targeted test files, not `-k`).

## Constraints / scope

### In scope
The operator surface only: `POST /api/v1/tasks/{task_id}/cancel` + CLI command +
api-client method + tests + docs. Enforcement already exists — do not rebuild it.

### Out of scope
Cancelling in-flight pending steps (v1's out-of-scope still holds); any
`task_status` VIEW or migration change.

### Budget
One task, `auto_merge: true`.

## sequence_of_work

```yaml
sequence_of_work:
  - id: task-cancel-command-v2
    title: Operator task-cancel (API + CLI) — publish via dispatcher.persist_and_publish
    workflow: wf-author
    intent: |
      Add an operator-facing task-cancel that emits the existing
      ``TaskCancelled`` event. The event + terminal enforcement ALREADY EXIST;
      add only the operator surface.

      *** THE CRITICAL FIX (v1 got this wrong): publish the event via
      ``dispatcher.persist_and_publish`` — exactly how ``create_task`` emits
      ``TaskRegistered``. Do NOT call ``dispatcher.dispatch_task`` and do NOT
      invent a separate publisher. ***

      Read first:
        * ``services/api/treadmill_api/routers/tasks.py`` — ``create_task``
          (~line 149-216): note its deps
          ``dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)]`` +
          ``session: Annotated[AsyncSession, Depends(get_session)]`` and its
          publish call ``await dispatcher.persist_and_publish(session,
          entity_type="task", action="registered", payload=TaskRegistered(...),
          plan_id=..., task_id=...)``. Also the ``retry`` endpoint for the
          request-model + 404/409 shape.
        * ``services/api/treadmill_api/events/task.py`` — ``TaskCancelled``
          (entity_type=task, action=cancelled, fields: ``reason: str | None``;
          it does NOT take task_id/repo in the payload — those go to
          ``persist_and_publish`` as kwargs).
        * ``services/api/treadmill_api/coordination/triggers.py`` —
          ``_TERMINAL_TASK_STATUSES = {pr_merged, cancelled, done}``.

      (1) API — add ``POST /api/v1/tasks/{task_id}/cancel`` with body
      ``TaskCancelRequest{reason: str (min_length=1, max_length=500)}``. Deps:
      ``session`` + ``dispatcher`` (mirror create_task). Logic:
        - load the task (404 if missing);
        - read its ``derived_status`` (same way ``get_task``/``TaskResponse``
          computes it); if it's in ``_TERMINAL_TASK_STATUSES`` → 409 (no event);
        - else publish EXACTLY:
          ``await dispatcher.persist_and_publish(session, entity_type="task",
          action="cancelled", payload=TaskCancelled(reason=body.reason),
          plan_id=task.plan_id, task_id=task.id)``;
        - return 200 ``{"task_id": str(task.id), "status": "cancelled"}``.
        - docstring: v1 marks the task terminal (gates future dispatch); does not
          cancel already-pending in-flight steps.

      (2) CLIENT — ``cli/treadmill_cli/api_client.py``: ``cancel_task(self,
      task_id, reason) -> dict`` (POST, raise ``ApiError`` on non-2xx, mirror
      ``retry_task``).

      (3) CLI — ``cli/treadmill_cli/cli.py``: ``task cancel`` command mirroring
      ``task retry`` (task_id arg + required ``--reason/-r``; 404 → "task not
      found", 409 → "task already terminal — nothing to cancel"; success →
      ``cancelled: <task_id>``).

      (4) TESTS — put them in NEW dedicated files so the validation script can
      target them exactly:
        * ``services/api/tests/test_task_cancel_endpoint.py``: cancelling a live
          task publishes a ``task.cancelled`` event (assert via the test's
          dispatcher/publish capture used by the create/retry endpoint tests) and
          returns 200; already-terminal → 409 + NO event; unknown id → 404.
        * ``cli/tests/test_cli_task_cancel.py``: ``task cancel`` invokes
          ``cancel_task`` with id+reason; 409 path prints the terminal message +
          non-zero exit.

      (5) DOCS (ADR-0030 — REQUIRED): ``services/api/AGENT.md`` (new cancel
      endpoint) + the CLI reference if one exists.
    scope:
      files:
        - services/api/treadmill_api/routers/tasks.py
        - cli/treadmill_cli/api_client.py
        - cli/treadmill_cli/cli.py
        - services/api/tests/test_task_cancel_endpoint.py
        - cli/tests/test_cli_task_cancel.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/alembic/versions/
    validation:
      - kind: deterministic
        description: |
          The cancel endpoint + CLI exist and their dedicated tests pass
          (targeted files, not -k, so no exit-5 on no-match).
        script: |
          cd services/api && uv run pytest tests/test_task_cancel_endpoint.py -q \
            && cd ../cli && uv run pytest tests/test_cli_task_cancel.py -q
```

## Risks / unknowns

- **derived_status read:** compute it the same way `get_task`/`TaskResponse`
  does (the `task_status` VIEW projection) — don't hand-roll a status query.
- **Old v1 task (`d39d5b55`) is quiesced** (no runs since 19:57); it won't
  conflict (empty branch). Once this ships, it can itself be cancelled with the
  new command.

## Post-mortem

_(filled when the plan completes)_
