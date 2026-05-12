---
status: drafting
trigger: ADRs 0025 + 0026 (proposed 2026-05-12); together they harden the trigger loop enough to trust autonomous execution
parent: docs/plans/2026-05-13-week-4-dev-local-deployment.md
---

# Plan: loop hardening (ADRs 0025 + 0026 implementation)

Two patterns cribbed from RAMJAC to close the duplicate-runs
failure mode that surfaced today:

- **ADR-0025**: per-worker heartbeat thread keeps SQS visibility
  extended during long Claude Code runs; messages aren't accidentally
  redelivered.
- **ADR-0026**: dispatch dedup table prevents legitimate-but-redundant
  trigger fan-out from spawning N wf-review runs on the same
  `(pr_number, head_sha)`.

Both ship together: each closes half of the loop. With them in place,
"Treadmill builds Treadmill" is trustworthy enough for autonomous
operation. Until then, the loop deathloops on pr_review_submitted /
pr_synchronize cascades.

`status: drafting` — operator review pass + ADR review pass before
flipping to active.

## Goal

After this plan executes:

1. A worker that takes >60s to process a message keeps its SQS lease
   alive via a daemon heartbeat thread (30s interval, 120s extension
   per ADR-0025). No more `ReceiptHandle has expired` errors.
2. A worker that fails (Claude returned non-zero, network blip,
   etc.) does NOT delete the message; SQS redelivers via visibility
   expiry; the message DLQs after maxReceiveCount.
3. The three Treadmill SQS queues' base `VisibilityTimeout` is set
   to 60s (was implicitly longer); the heartbeat carries the lease.
4. A new `workflow_dispatch_dedup` table tracks per-`(workflow_id,
   discriminator)` dispatches. The trigger evaluator inserts a
   dedup row before creating the workflow_run; PK collision → skip
   dispatch.
5. wf-review, wf-feedback, wf-ci-fix, wf-conflict workflows all have
   dedup-key builders. wf-author + wf-plan opt out (existing
   mechanisms handle their dedup).
6. The o11y plan-chain that hit duplicate-run problems today can
   re-execute cleanly: a single wf-review per (PR, SHA); a single
   wf-feedback per review_id; no cascade.

## Constraints / scope

### In scope

- Heartbeat thread + don't-delete-on-error refactor in
  `workers/agent/treadmill_agent/runner.py`.
- Base visibility-timeout updates in
  `infra/treadmill_infra/constructs/{messaging,webhook_receiver}.py`.
- New `workflow_dispatch_dedup` table + alembic migration.
- Dedup-key builders + the insert-first / dispatch-second flow in
  the trigger evaluator (`coordination/consumer.py` + siblings).
- Tests for both halves.
- Operator-runbook updates: how to inspect the DLQ; how to manually
  clear a dedup row.

### Out of scope

- A "force re-review" CLI flag (deferred per ADR-0026 §"What this
  does NOT do" + Q26.d).
- Cleanup / TTL of old dedup rows (Q26.b — defer).
- Pushing dedup config into the `event_triggers` table (Q26.e —
  defer).
- Ralph-loop validation's own dedup discriminator (its own ADR will
  add the `attempt` counter to the same table).
- Webhook-layer dedup (before SQS enqueue). The trigger-layer dedup
  in this plan is enough at v0.

## Sequence of work

```yaml
sequence_of_work:
  - id: heartbeat-thread-runner
    title: Worker heartbeat thread + don't-delete-on-error per ADR-0025
    workflow: wf-author
    intent: |
      Refactor ``workers/agent/treadmill_agent/runner.py``'s
      message-handling block so each in-flight message gets a
      daemon heartbeat thread that extends visibility every 30s
      by 120s (constants ``HEARTBEAT_INTERVAL_SECONDS = 30`` and
      ``VISIBILITY_EXTENSION_SECONDS = 120`` — RAMJAC values
      adopted verbatim per ADR-0025).

      Pattern (RAMJAC line-for-line):

        stop_event = threading.Event()
        heartbeat = threading.Thread(
            target=_run_heartbeat,
            args=(sqs_client, queue_url, receipt_handle, stop_event),
            daemon=True,
        )
        heartbeat.start()
        try:
            result = _execute(ctx)
            sqs_client.delete_message(QueueUrl=queue_url,
                                      ReceiptHandle=receipt_handle)
        except Exception:
            # DO NOT delete — let SQS expire + redeliver + DLQ
            logger.exception("worker step failed; leaving message in flight")
            raise
        finally:
            stop_event.set()
            heartbeat.join(timeout=5)

      ``_run_heartbeat`` is a small function that loops
      ``stop_event.wait(30)``, calls
      ``change_message_visibility(VisibilityTimeout=120)``, logs
      warnings on failure (don't crash the thread; the lease will
      expire if AWS is unreachable for >60s).

      The on-error path's existing call to ``_delete`` is removed.
      Worker exceptions propagate to the runner's top-level which
      logs + exits non-zero; the message stays in flight; SQS
      redelivers per maxReceiveCount=5 → DLQ.

      Tests:
        - The heartbeat thread starts before the disposition
          dispatch + stops after.
        - ``change_message_visibility`` is called at least once on
          a simulated >35s work block (mock ``time.sleep`` to
          accelerate; assert the call count + arguments).
        - On disposition raise, ``delete_message`` is NOT called.
        - ``stop_event.set()`` runs in the ``finally`` block even
          on exception.
    scope:
      files:
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_runner.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          A mocked worker run of 90s (simulated by clock-advance)
          produces at least 2 ``change_message_visibility`` calls
          with ``VisibilityTimeout=120``. A mocked worker run that
          raises produces ZERO ``delete_message`` calls.

  - id: queue-visibility-timeout-cdk
    title: Set base visibility-timeout to 60s on all Treadmill queues
    workflow: wf-author
    depends_on:
      - task.heartbeat-thread-runner.pr_merged
    intent: |
      Update the CDK constructs to set ``visibility_timeout=60``
      seconds on all three Treadmill queues:
        - ``treadmill-<id>-work.fifo`` (in messaging.py)
        - ``treadmill-<id>-coordination`` (in messaging.py)
        - ``treadmill-<id>-webhook-inbox`` (in webhook_receiver.py)

      The heartbeat from the previous task carries the lease for
      long work; 60s is the death-detection window for workers
      that die without stopping the heartbeat.

      Update the corresponding tests
      (``infra/tests/test_messaging_construct.py``,
      ``test_webhook_receiver_construct.py``) to assert the new
      ``VisibilityTimeout`` value.

      Operator-runbook note: existing deployments need a
      ``cdk deploy`` to pick up the new visibility values. Old
      queues continue to operate with their existing timeout
      until the redeploy.
    scope:
      files:
        - infra/treadmill_infra/constructs/messaging.py
        - infra/treadmill_infra/constructs/webhook_receiver.py
        - infra/tests/test_messaging_construct.py
        - infra/tests/test_webhook_receiver_construct.py
    validation:
      - kind: deterministic
        description: |
          ``cdk synth`` against the test deployment produces queue
          resources with ``VisibilityTimeout: 60`` on all three
          Treadmill queues.

  - id: dedup-table-migration
    title: Add workflow_dispatch_dedup table + model
    workflow: wf-author
    depends_on:
      - task.queue-visibility-timeout-cdk.pr_merged
    intent: |
      New alembic migration creating:

        CREATE TABLE workflow_dispatch_dedup (
            dedup_key       text   NOT NULL,
            workflow_run_id uuid   NOT NULL,
            dispatched_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (dedup_key)
        );

      Note per ADR-0026 §"Optimistic pre-check + PK gate ordering":
      ``workflow_run_id`` is a uuid but does NOT carry a FK to
      ``workflow_runs(id)`` — relaxed for v0 to support
      insert-first/dispatch-second ordering without deferrable
      constraints.

      New SQLAlchemy model
      ``services/api/treadmill_api/models/workflow_dispatch_dedup.py``
      mirroring the table.

      Tests in ``services/api/tests/`` for the model + migration
      idempotency.
    scope:
      files:
        - services/api/alembic/versions/0009_workflow_dispatch_dedup.py
        - services/api/treadmill_api/models/workflow_dispatch_dedup.py
        - services/api/treadmill_api/models/__init__.py
        - services/api/tests/test_dispatch_dedup_migration.py
    validation:
      - kind: deterministic
        description: |
          ``alembic upgrade head`` against a fresh database creates
          the ``workflow_dispatch_dedup`` table with the PK on
          ``dedup_key``. Re-running ``upgrade head`` is a no-op.

  - id: dedup-builders-and-trigger-integration
    title: Per-workflow dedup-key builders + insert-first dispatch flow
    workflow: wf-author
    depends_on:
      - task.dedup-table-migration.pr_merged
    intent: |
      New module
      ``services/api/treadmill_api/coordination/dispatch_dedup.py``
      with the ``DEDUP_KEY_BUILDERS`` dict per ADR-0026's table:

        DEDUP_KEY_BUILDERS: dict[str, Callable[[Event], str | None]] = {
            "wf-review":   lambda e: f"wf-review:{e.repo}:pr={e.pr_number},sha={e.head_sha}",
            "wf-feedback": lambda e: f"wf-feedback:{e.repo}:review={e.review_id}",
            "wf-ci-fix":   lambda e: f"wf-ci-fix:{e.repo}:check_run={e.check_run_id}",
            "wf-conflict": lambda e: f"wf-conflict:{e.repo}:pr={e.pr_number},sha={e.head_sha}",
            # wf-author and wf-plan opt out (return None).
        }

      Plus a ``maybe_dispatch_with_dedup(session, event, workflow_id,
      ...)`` helper that:
        1. Builds the dedup_key. If None, fall through to
           unconditional dispatch (existing behavior).
        2. INSERT INTO workflow_dispatch_dedup ... in a SAVEPOINT
           or sub-transaction.
        3. Catch IntegrityError → skip dispatch + log INFO.
        4. On insert success, proceed with the existing
           workflow_run creation.
        5. UPDATE the dedup row's workflow_run_id once the run
           exists (so the dedup row points at its actual run).

      Wire the helper into the trigger evaluator's call sites in
      ``coordination/consumer.py`` (or its trigger-handler
      siblings — find the existing ``dispatch_task`` callers for
      each workflow).

      Tests:
        - Each builder produces the expected dedup key for a
          synthetic event.
        - Same event arriving twice → second dispatch is skipped;
          the dedup_key row exists once.
        - Workflows that opt out (return None) dispatch every time.
        - The IntegrityError path is exercised (concurrent insert
          stub).
    scope:
      files:
        - services/api/treadmill_api/coordination/dispatch_dedup.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_dispatch_dedup.py
        - services/api/tests/test_integration_dispatch_dedup.py
    validation:
      - kind: deterministic
        description: |
          A unit test fires the same ``pr_review_submitted`` event
          twice through ``maybe_dispatch_with_dedup``; the first
          call creates a wf-feedback run + a dedup row; the second
          call skips dispatch (no new run) and logs the duplicate.

  - id: dlq-runbook-and-cleanup-docs
    title: Operator runbook for DLQ inspection + dedup-row cleanup
    workflow: wf-author
    depends_on:
      - task.dedup-builders-and-trigger-integration.pr_merged
    intent: |
      Document two operator runbooks in
      ``docs/plans/2026-05-13-week-4-dev-local-deployment.md``
      (or sibling) under a new "DLQ + dedup runbook" section:

      **DLQ inspection** (per ADR-0025's don't-delete-on-error):
      - List DLQ contents:
        ``aws sqs receive-message --queue-url
        $(yq .aws.work_queue_dlq_url ~/.treadmill/personal.yaml)
        --max-number-of-messages 10 --profile treadmill-personal``
      - Inspect the message body to understand why it poisoned.
      - Either purge (``aws sqs purge-queue``) or replay
        (manually re-enqueue to the main queue after fixing the
        root cause).

      **Dedup row cleanup** (per ADR-0026's manual override):
      - Find the dedup row blocking a re-dispatch:
        ``docker exec treadmill-postgres psql -U postgres -d
        treadmill -c "SELECT * FROM workflow_dispatch_dedup
        WHERE dedup_key LIKE 'wf-review:joeLepper/treadmill%';"``
      - Delete the row to allow a fresh dispatch:
        ``DELETE FROM workflow_dispatch_dedup WHERE
        dedup_key = '<key>';``
      - Trigger the dispatch by re-firing the source event (push
        a trivial commit, post a fresh review, etc.).
    scope:
      files:
        - docs/plans/2026-05-13-week-4-dev-local-deployment.md
    validation:
      - kind: deterministic
        description: |
          The Week-4 plan's running log contains a new section
          titled "DLQ + dedup runbook" (or similar) with both the
          DLQ-inspection and dedup-cleanup commands shown.
```
