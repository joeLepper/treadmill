---
status: drafting
trigger: two friction points observed during the o11y plan-merge smoke 2026-05-12; both are now-problems
parent: docs/plans/2026-05-13-week-4-dev-local-deployment.md
---

# Plan: plan-doc trigger resilience

Two friction points hit live during the o11y plan-merge smoke
2026-05-12. Both have the same shape: the plan-doc trigger handler
ran but couldn't complete its work, and the error was caught + logged
but the `pr_merged` SQS message was deleted anyway — so the trigger
silently lost the merge. The operator's workaround was a re-merge.
That's not a tenable failure mode.

`status: drafting` — operator review pass before flipping to active.

## The two friction points

**FP1 — Workflow seed race.** When `treadmill-local up` cycles a
fresh Postgres (every `down`/`up`), the workflow table is empty
until the operator runs `treadmill workflows seed-starters`. If a
`pr_merged` event hits the queue during that window (e.g., a PR
merged while the stack was cycled), the plan-doc trigger handler
raises `400: workflow 'wf-author' not registered`; the event is
dropped. Reproduced by PR #7's merge during today's smoke.

**FP2 — `depends_on:` syntax trap.** The plan-doc parser accepts
`depends_on:` only in the form `task.<id>.pr_merged` (or
`.run.completed` / `.step.<name>.completed`). Bare task IDs like
`- otel-sdk-foundation` are rejected with a parser error. The error
message is clear but every example I've seen in our existing docs
uses the bare-ID shorthand, which the parser doesn't accept.
Reproduced by PR #8's merge.

Neither is fundamentally architectural — both are operator-experience
gaps. Both should be fixable by a wf-author run.

## Goal

After this plan executes:

1. `treadmill-local up` ensures the workflow table is seeded before
   the consumer + webhook poller start processing events. Either
   `up` auto-runs `seed-starters` after the API is healthy, OR the
   API self-seeds on startup if the workflows table is empty.
2. The plan-doc parser accepts `depends_on:` in either form: the
   full expression (`task.<id>.pr_merged`) AND the bare-ID shorthand
   (which defaults to `.pr_merged`). The bare form is the operator-
   ergonomic default; the full form is preserved for cases where
   the operator wants `.run.completed` or `.step.<name>.completed`
   semantics.
3. The plan-doc trigger handler's error handling is sharper: when a
   `pr_merged` event can't be processed cleanly (workflow missing,
   parse error, etc.), the handler either retries with bounded
   backoff OR puts the SQS message back for re-delivery. Silent loss
   is the failure mode we're explicitly inverting.

## Constraints / scope

### In scope

- Auto-seed workflows during `treadmill-local up` (FP1).
- Forgiving `depends_on:` parser (FP2).
- Sharper failure semantics for the plan-doc trigger handler when
  preconditions aren't met (workflows missing, etc.) — at minimum,
  re-queue rather than drop.

### Out of scope

- Auto-seed for non-workflow seed data (roles, event_triggers).
  Today those seed cleanly via `seed-starters` too; the same
  auto-seed step covers them.
- Migration of existing plan docs to the bare-ID syntax (they're
  already on the full form; both work after this lands).
- A general "retry with backoff" framework for the consumer's
  trigger handlers. The sharper semantics here are scoped to the
  plan-doc handler specifically; future trigger handlers (per the
  forthcoming auto-deploy ADR + the existing event_triggers
  machinery) get the same treatment one-by-one as needed.

## Sequence of work

```yaml
sequence_of_work:
  - id: auto-seed-on-up
    title: treadmill-local up auto-seeds workflows after API is healthy
    workflow: wf-author
    intent: |
      Extend ``tools/local-adapter/treadmill_local/runtime.py`` so
      ``treadmill-local up`` (in both fully-local and dev-local
      modes) waits for the API container's ``/health/ready`` to
      report ``status: ok``, then POSTs the starter seed via the
      API's HTTP surface (effectively running the equivalent of
      ``treadmill workflows seed-starters``).

      Pseudocode:
        - After ``_start_services()`` returns, poll
          ``GET http://localhost:<port>/health/ready`` with a 30s
          deadline; backoff between polls.
        - When status==ok, invoke the seed endpoint (the
          equivalent of ``treadmill workflows seed-starters`` —
          look at the CLI's implementation to know the exact API
          surface; probably a POST per role + workflow, idempotent
          via 409-on-conflict).
        - Print a brief "seeded N workflows" line in the up flow's
          progress block.
        - On failure (API doesn't come healthy, seeding hits a 5xx
          twice in a row): print a clear error + exit non-zero.
          Don't silently leave a partially-seeded DB.

      A ``--no-auto-seed`` flag on ``up`` lets the operator skip
      the seed step (useful for tests + for debugging seed
      regressions).

      Tests in
      ``tools/local-adapter/tests/test_runtime_dev_local.py``:
        - happy path: API health probe succeeds, seed POSTs fire,
          progress message printed.
        - flag opts out: ``--no-auto-seed`` skips the entire step.
        - API never healthy: clean error, no infinite loop.
        - Seed fails: clean error.
    scope:
      files:
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/treadmill_local/cli.py
        - tools/local-adapter/tests/test_runtime_dev_local.py
        - tools/local-adapter/tests/test_image_build.py
    validation:
      - kind: deterministic
        description: |
          After ``treadmill-local up --deployment <id>`` completes
          on a fresh Postgres, ``GET
          http://localhost:8088/api/v1/workflows`` returns the 7
          starter workflows. No operator-side
          ``treadmill workflows seed-starters`` invocation
          required.

  - id: depends-on-bare-id-shorthand
    title: Plan-doc parser accepts bare task IDs in depends_on
    workflow: wf-author
    depends_on:
      - task.auto-seed-on-up.pr_merged
    intent: |
      Edit
      ``services/api/treadmill_api/parsers/plan_doc.py`` so the
      ``depends_on`` field accepts both the explicit-gate form
      (``task.<id>.pr_merged`` / ``.run.completed`` /
      ``.step.<name>.completed``) AND the bare-task-id shorthand
      (``<id>``). Bare IDs are interpreted as
      ``task.<id>.pr_merged`` — the most common case (downstream
      task waits for predecessor's PR to merge).

      Concretely, the validation logic in (probably) the dispatcher
      or the plan-doc model gets a normalization pass: any
      depends_on entry that doesn't start with ``task.`` is
      rewritten as ``task.<id>.pr_merged`` before storage.

      Stored form in the DB is the explicit-gate string; the bare
      shorthand is purely an authoring affordance.

      Document the shorthand in the plan-doc author's reference
      (probably in ADR-0010 or a sibling doc) so future plan docs
      can use either form.

      Tests in
      ``services/api/tests/test_plan_doc_parser.py``:
        - bare ID is normalized to ``task.<id>.pr_merged``.
        - explicit form passes through unchanged.
        - mixed list (some bare, some explicit) works as a unit.
        - invalid forms (e.g.,
          ``task.foo.unknown_action``) still raise the existing
          parser error.
    scope:
      files:
        - services/api/treadmill_api/parsers/plan_doc.py
        - services/api/tests/test_plan_doc_parser.py
        - docs/adrs/0010-plan-rooted-task-hierarchy.md
    validation:
      - kind: deterministic
        description: |
          A plan doc with ``depends_on: [other-task]`` parses
          successfully and stores the dependency as
          ``task.other-task.pr_merged``. Same plan doc with
          ``depends_on: [task.other-task.pr_merged]`` produces the
          same stored form.

  - id: plan-doc-trigger-failure-semantics
    title: Plan-doc trigger handler doesn't silently lose merges on transient failures
    workflow: wf-author
    depends_on:
      - task.depends-on-bare-id-shorthand.pr_merged
    intent: |
      Today's handler in
      ``services/api/treadmill_api/coordination/plan_doc_trigger.py``
      catches exceptions broadly and logs them at ERROR, but the
      consumer above it deletes the SQS message regardless of
      handler outcome. Result: when the handler fails (workflows
      missing, parse error, etc.), the merge event is lost.

      Replace the broad catch with categorized handling:

        - **Transient failure** (workflow missing, DB connection
          error, expired credentials, network error reaching gh API):
          the handler does NOT mark the message as processed.
          Re-raise; the consumer's outer handling re-queues with
          backoff. After N retries, the message DLQ's per ADR-0017.
        - **Permanent failure** (plan-doc parse error, doc fetch
          returned 404, status: drafting): the handler persists a
          ``plan_doc.parse_failed`` / ``observed_inactive`` event
          and ACKs the SQS message — there's no recovery via retry.
        - **Success**: the handler creates the Plan + dispatches
          tasks + ACKs.

      The classifier is small (a single function mapping exception
      types to ``retry | permanent | success``); plug it into
      handle_pr_merged + the consumer's swallow-vs-ACK behavior.

      Add a deterministic test that simulates each category:
        - Workflow missing (transient) → exception propagates;
          SQS message stays.
        - Plan-doc parse error (permanent) → ``plan_doc.parse_failed``
          event persisted; SQS message ACKed.
        - Status:drafting (permanent) → ``observed_inactive`` event
          persisted; SQS message ACKed.
        - Happy path → Plan + tasks created; SQS message ACKed.
    scope:
      files:
        - services/api/treadmill_api/coordination/plan_doc_trigger.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_plan_doc_trigger.py
        - services/api/tests/test_integration_plan_doc_merge.py
    validation:
      - kind: deterministic
        description: |
          With workflows-missing as the failure mode, the SQS
          message remains in the queue (not deleted) after one
          handler invocation; with plan-doc parse failure, the
          message is deleted and the ``plan_doc.parse_failed``
          event row exists.
```
