# ADR-0025: Worker visibility-timeout heartbeat thread

- **Status:** proposed
- **Date:** 2026-05-12
- **Related:** ADR-0017 (webhook ingestion + SQS queue config), ADR-0011 (immutable runtime), ADR-0022 (per-kind worker dispatch); cribs the RAMJAC vLLM heartbeat pattern verbatim

## Context

A Treadmill worker picks up a message, processes it (often >30s,
sometimes >2 min for Claude Code workloads), then tries to
`sqs:DeleteMessage` to ack. AWS rejects with:

```
botocore.exceptions.ClientError: An error occurred
(InvalidParameterValue) when calling the DeleteMessage operation:
Value <handle> for parameter ReceiptHandle is invalid. Reason: The
receipt handle has expired.
```

The message's visibility timeout elapsed while the worker was
running. SQS considers the message un-acked → re-delivers it → a new
worker spawns + processes the same message. Duplicate side-effects:

- Two PR reviews on the same SHA.
- Two PR comments.
- Two wf-author runs trying to push the same commit (one fails on
  push-conflict; the second has to retry).
- Double Claude credits spent.

Observed live 2026-05-12 during the o11y plan-merge chain: a
wf-feedback worker completed cleanly but couldn't ack; PR #10
ended up with two near-identical wf-feedback runs (one in flight,
one redelivered) producing duplicate noise. Captured as task #105.

**The root cause is the worker doing >`visibility_timeout` of work
without telling SQS it's still alive.** SQS supports
`ChangeMessageVisibility` for exactly this case: the in-flight
worker periodically extends its lease.

RAMJAC (`/home/joe/ramjac/service/vllm_server/rpc_server.py`
lines 184-284) ships the pattern verbatim: a dedicated daemon
heartbeat thread per in-flight message, extends visibility every 30s
by 120s, joined in a `finally` block. Base queue visibility is
intentionally short (60s) — the heartbeat is what keeps the lease
alive.

Treadmill cribs it.

## Decision

### Per-message daemon heartbeat thread

When a Treadmill worker receives a message, it immediately spawns a
daemon thread that extends visibility on a fixed cadence. The thread
runs until the message is processed (success or failure); a
`finally` block stops the thread + joins it.

Mechanism (RAMJAC line-for-line adopted):

```python
HEARTBEAT_INTERVAL_SECONDS = 30
VISIBILITY_EXTENSION_SECONDS = 120


def _run_heartbeat(
    sqs_client, queue_url, receipt_handle, stop_event
) -> None:
    while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            sqs_client.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=VISIBILITY_EXTENSION_SECONDS,
            )
        except Exception:
            logger.warning(
                "Failed to extend message visibility timeout",
                exc_info=True,
            )


def _process_message(...) -> None:
    stop_event = threading.Event()
    heartbeat = threading.Thread(
        target=_run_heartbeat,
        args=(sqs_client, queue_url, receipt_handle, stop_event),
        daemon=True,
    )
    heartbeat.start()
    try:
        result = _execute(...)
        sqs_client.delete_message(
            QueueUrl=queue_url, ReceiptHandle=receipt_handle,
        )
    except Exception:
        # Do NOT delete — let visibility timeout expire → SQS
        # redelivers → DLQ after maxReceiveCount.
        logger.exception("worker step failed; leaving message in flight")
        raise
    finally:
        stop_event.set()
        heartbeat.join(timeout=5)
```

The heartbeat lives in `workers/agent/treadmill_agent/runner.py`'s
message-handling block (between SQS receive and the disposition
dispatch). It wraps the entire per-message work — clone, Claude Code
invocation, dispatch handler, the lot.

### Queue visibility-timeout becomes intentionally short

Today the work queue's `VisibilityTimeout` is sized to "longest
possible work" — implicitly several minutes. After this ADR, base
visibility becomes **60 seconds** and the heartbeat carries it. This
gives a clean failure mode: a worker that *dies* mid-job (segfault,
OOM, container exit) stops heartbeating; SQS expires the message
within the next ~60s; a fresh worker picks it up.

The CDK config for the work queue + the events queue + the
webhook-inbox queue gets the new `visibility_timeout=60s`.

### Don't-delete-on-error

The current runner's exception-handling path: `_handle_step` catches
exceptions broadly, logs at ERROR, then calls `_delete` (per
`workers/agent/treadmill_agent/runner.py`). That ack-on-error pattern
means a worker that fails mid-step still acks the message. SQS sees
the message processed; the operator never sees the retry.

Replace with: on worker failure, **do not call `_delete`**. The
message stays in flight; visibility-timeout (now ~60s) expires;
SQS redelivers; another worker tries; after `maxReceiveCount=5`
the message goes to the DLQ.

This is RAMJAC's `RetryableError` pattern. The runner already
distinguishes worker-side errors (e.g., Claude returned non-zero)
from infrastructure errors (boto3 ClientError); the latter should
auto-retry. The former should also auto-retry (let SQS redelivery
give Claude another chance) at v0; if we observe poison messages
(same SHA failing 5 times → DLQ), the operator inspects.

### No new tables

The heartbeat is purely in-memory + AWS-side. No DB writes. State is
the (thread, stop_event) tuple owned by the worker process.

### Composability with ADR-0018 (autoscaler)

Workers are one-shot (`EXIT_AFTER_STEP=true` per ADR-0018). One
message → one worker → one heartbeat thread → exit. No long-running
worker process to supervise multiple messages. The heartbeat scope
matches the worker lifetime, which matches the message lifetime.

### Composability with ADR-0022 (output kinds)

The heartbeat lives in the shared prefix of the runner — before the
disposition dispatch. All output kinds (code, review, analysis,
plan_doc) inherit the heartbeat. No per-kind variation.

## Bunkhouse precedent

Bunkhouse's worker is also long-running ECS Fargate but uses a
different SQS pattern (max-receive-count + long polling without
explicit heartbeats — relies on the queue's high `VisibilityTimeout`
default). That worked for bunkhouse's workloads where work was
typically <30s. Treadmill's Claude Code workloads are too long for
the static-timeout approach; the heartbeat is a deliberate divergence
toward the RAMJAC pattern.

## Trade-offs

- **One more thread per worker.** A daemon thread with a single
  `time.sleep` is cheap; daemon=True means it doesn't block exit.
- **One more boto3 call every 30s during a long worker run.** Free
  under SQS's first-million-per-month tier; bounded by run
  duration.
- **Worker code gains a thread-safety constraint.** The heartbeat
  thread shares the `receipt_handle` + `sqs_client` with the main
  thread. Both are read-only after creation; no concurrent
  modification. Document this constraint in the runner module.
- **Failure mode shifts.** Today's "worker dies + message acked +
  silent loss" becomes "worker dies + message redelivers + next
  worker tries + eventually DLQs." The DLQ is the new operator-
  visible signal for "this message can't be processed." Make sure
  the operator runbook knows where to look.
- **Visibility timeout choice (60s base / 120s extension).** RAMJAC
  uses 60/120; we adopt. Bigger numbers mean longer time-to-detect a
  dead worker; smaller numbers mean more API calls. 60/120 is the
  proven balance.

## Alternatives considered

- **Set a very long static visibility timeout (e.g., 10 minutes).**
  Works if you know the max worker runtime. Fragile: a worker that
  takes 11 minutes still fails; a worker that dies at minute 1 still
  blocks redelivery for 9 minutes. Rejected: bounded statics don't
  fit Claude Code's variance.
- **Long-poll on each tick + extend visibility inline.** Worker
  itself extends visibility between work units instead of a
  background thread. Less clean (mixing operational concerns into
  the work flow); RAMJAC explicitly migrated away from this
  pattern (Flavor B → Flavor A). Rejected.
- **Use SQS FIFO + MessageDeduplicationId for the work queue.** FIFO
  + dedup-ids prevent re-delivery for a 5-minute window when the
  same dedup-id is enqueued. Not the right tool: the issue is
  visibility expiry, not duplicate enqueues. The work queue is
  already FIFO for ordering reasons; this doesn't solve the
  expiry problem.
- **Re-architect to short-lived workers.** Cap each worker at 30s
  of work, chunk longer jobs. Doesn't survive Claude Code's
  reality (a single Claude run is the natural unit). Rejected.

## Open questions

- **Q25.a — What about the API's consumer + webhook-inbox poller?**
  Both run inside the API container as async tasks. Their messages
  are typically processed in <1s (no Claude calls). Do they need
  the heartbeat? Probably not. But the consumer's trigger handlers
  do call into work that can take seconds (gh API fetches in
  `plan_doc_trigger.py`). If a trigger handler ever exceeds the
  consumer's visibility timeout, same failure mode. Defer until
  observed; the consumer's typical receive→ack window is <5s.
- **Q25.b — DLQ alerting?** With don't-delete-on-error, the DLQ
  becomes the operator-visible signal for poison messages. ADR-0020's
  observability stack should surface DLQ depth as a Grafana metric
  + alert. Track as a follow-up.
- **Q25.c — Heartbeat failures.** If `change_message_visibility`
  fails (transient AWS error, expired creds), the worker logs the
  warning and continues. The lease will eventually expire if the
  failures persist; the next redelivery picks up. Is that the right
  behavior? Yes — alternative is panic-and-die, which is worse
  (loses work-in-progress for transient AWS hiccups).
- **Q25.d — Worker death detection.** With a 60s base visibility, a
  worker that dies takes up to 60s for SQS to expire its lease. Is
  that acceptable? Yes for v0; if we need faster recovery, the
  autoscaler could supplement with container-died → SIGKILL-
  message-with-zero-visibility tooling. Out of scope.

## Consequences

- **`workers/agent/treadmill_agent/runner.py`**: refactor
  `_handle_step` to wrap work in the heartbeat-thread pattern.
  Remove the on-error `_delete` call so failures redeliver.
- **`workers/agent/treadmill_agent/runner.py`**: new constants
  `HEARTBEAT_INTERVAL_SECONDS = 30` and
  `VISIBILITY_EXTENSION_SECONDS = 120`.
- **CDK changes**: `infra/treadmill_infra/constructs/messaging.py`
  + `webhook_receiver.py` update `visibility_timeout` to 60s on all
  three queues (work, coordination, webhook-inbox).
- **Tests**: `workers/agent/tests/test_runner.py` gains heartbeat
  tests using a fake `sqs_client` that records
  `change_message_visibility` calls; assert at least one call after
  a 30-second-simulated work block.
- **Tests**: `workers/agent/tests/test_runner.py` updates the
  on-error path: assert `_delete` is NOT called when the disposition
  raises.
- **Operator runbook**: document the don't-delete-on-error
  semantics + how to inspect the DLQ.
- **Pairs with ADR-0026** (dispatch dedup): together they close the
  duplicate-runs failure mode. Heartbeat prevents accidental
  redelivery; dedup prevents legitimate-but-redundant dispatches.
- **Phase 2 self-driving criterion**: workers running Claude Code
  jobs for >2 minutes no longer cause duplicate runs.

## Diagram

Each in-flight SQS message gets a dedicated daemon heartbeat thread that extends visibility every 30s by 120s. Base queue visibility is intentionally short (60s) so a worker that dies mid-job stops heartbeating and the lease expires quickly. On worker error, the message is NOT deleted — visibility expiry drives re-delivery; after `maxReceiveCount=5` it lands in the DLQ. On success, `_delete` happens in the same `finally` block that stops + joins the heartbeat.

```mermaid
sequenceDiagram
    participant SQS as AWS SQS (work queue, visibility=60s)
    participant Runner as Worker runner (one-shot container)
    participant Heartbeat as Heartbeat daemon thread
    participant Disposition as Disposition handler (code / review / analysis / plan_doc)
    participant Claude as Claude Code (long-running)
    participant DLQ as Work-queue DLQ

    Runner->>SQS: receive_message (wait long-poll)
    SQS-->>Runner: message {receipt_handle, payload}
    Runner->>Heartbeat: start daemon thread (stop_event, receipt_handle)
    Runner->>Disposition: dispatch (per ADR-0022 output_kind)
    Disposition->>Claude: claude code <invocation>
    loop every 30s while work in flight
        Heartbeat->>SQS: change_message_visibility(120s)
        alt extend succeeded
            SQS-->>Heartbeat: ok
        else extend failed (transient)
            SQS-->>Heartbeat: ClientError
            Heartbeat-->>Heartbeat: log warning, continue
        end
    end
    alt disposition succeeded
        Claude-->>Disposition: result
        Disposition-->>Runner: ok
        Runner->>SQS: delete_message(receipt_handle)
        Runner->>Heartbeat: stop_event.set() + join(timeout=5)
    else disposition raised
        Claude-->>Disposition: error
        Disposition-->>Runner: exception (do NOT delete)
        Runner->>Heartbeat: stop_event.set() + join(timeout=5)
        Runner-->>SQS: heartbeat stops; visibility expires after 60s
        SQS-->>SQS: redeliver up to maxReceiveCount=5
        SQS-->>DLQ: message moves to DLQ
    else worker process died (segfault / OOM / container exit)
        Heartbeat-->>Heartbeat: thread dies with process (daemon=True)
        SQS-->>SQS: no more heartbeats; visibility expires (~60s)
        SQS-->>SQS: redeliver to a fresh worker
    end
```

Note: the heartbeat lives in the shared prefix of the runner — before the per-kind disposition dispatch — so all output kinds (ADR-0022) inherit it without per-kind variation. The `receipt_handle` and `sqs_client` are read-only after thread creation; that's the thread-safety contract.
