# Zero consume rate — workers exist but aren't completing work

**Related:** ADR-0075 (operator obligations), `project_worker_stale_lease`

## Symptom

Tasks are dispatched and visible in `treadmill task list`, but they remain stuck in a workflow step with status `pending` or `executing`. The workflow_run_steps row shows no `started_at`, or the step has been started for an unusually long time with no `completed_at`. Worker processes exist in the fleet but the queue is not draining.

## Root cause checklist

Check these in order:

- [ ] **Queue URL mismatch**: The worker is consuming from a different SQS queue than the one the task published to. Verify `TREADMILL_QUEUE_URL` env var on running workers matches the value in your deployment.

- [ ] **IAM drift**: Worker assumes an IAM role that no longer has `sqs:ReceiveMessage` or `sqs:DeleteMessage` permissions on the queue. Check CloudWatch Logs for `AccessDenied` errors in worker logs.

- [ ] **Worker process crash-loop**: Worker starts but crashes immediately after lease acquisition, before it can process the message. Check:
  - Worker process logs (in `.treadmill-local/worker-*.log` for dev-local, or CloudWatch for cloud deployments)
  - Whether `started_at` is ever set (if yes, the worker did start but crashed; if no, it never started)

- [ ] **Lease expiry without renewal**: Worker acquires a message but doesn't extend the visibility timeout before the default 30s expires. The message re-enters the queue, but the worker is still holding a local copy and crashes when it tries to delete it. Check worker logs for visibility timeout or `InvalidParameterValue` errors.

- [ ] **Backpressure or deadlock**: The worker acquired the message but is blocked waiting for something (database, API call, lock contention). Check:
  - Whether the database is accepting connections (connection pool exhaustion, stale connections)
  - Whether any downstream service the worker depends on is unhealthy
  - Worker logs for `Timeout` or `deadline exceeded` errors

## Commands

**Inspect workflow_run_steps for the stalled task:**

```sql
select step_name, role_id, status, started_at, completed_at, created_at
from workflow_run_steps wrs
join workflow_runs wr on wr.id = wrs.run_id
where wr.task_id = '<uuid>'
order by wrs.id desc limit 10;
```

If `status='pending'` and `started_at is null`, no worker picked up the step yet.

If `started_at is not null` and `completed_at is null`, the worker picked it up but hasn't finished.

**Check queue depth and visibility timeout:**

```bash
aws sqs get-queue-attributes \
  --queue-url "$TREADMILL_QUEUE_URL" \
  --attribute-names All
```

Look for `ApproximateNumberOfMessages` (should decrease over time if workers are draining) and `VisibilityTimeout` (default 30s; too small can cause re-queueing).

**Inspect worker logs (dev-local):**

```bash
tail -f .treadmill-local/worker-*.log
# Look for RuntimeError, timeout, AccessDenied, or connectivity errors
```

**Check worker process existence:**

```bash
ps aux | grep treadmill-worker
# Verify workers are actually running, not in a crash-loop
```

**Verify IAM permissions:**

```bash
aws iam get-role-policy \
  --role-name "<worker-role>" \
  --policy-name "<inline-policy-name>"
```

Ensure the policy includes:
```json
{
  "Effect": "Allow",
  "Action": [
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:ChangeMessageVisibility"
  ],
  "Resource": "<queue-arn>"
}
```

## Durable fix

**Short term (unblock the queue):**

1. Kill all running workers: `pkill -f treadmill-worker` (or scale down the deployment in cloud).
2. Wait 60s for messages to re-enter the queue (visibility timeout window).
3. Restart workers with corrected configuration (fixed queue URL, refreshed IAM creds, fixed crash-loop root cause).
4. Verify new messages are being consumed: `aws sqs get-queue-attributes` should show `ApproximateNumberOfMessages` decreasing.

**Long term (prevent recurrence):**

- **For queue URL mismatch**: Pin the queue URL in a config file or secrets manager, not an env var that can drift. CI/CD deploy should validate worker and dispatcher queue URLs match before deploying.

- **For IAM drift**: Add a worker health-check that runs every N seconds and verifies `sqs:ReceiveMessage` on a dummy message. If it fails, escalate via `system_event` (ADR-0071 significant set) or operator-relay.

- **For crash-loops**: Add a pre-worker startup check in the worker's launcher that verifies required dependencies (database, API endpoints) are reachable. Fail the worker startup early if not, so the fleet doesn't spawn workers that will crash.

- **For visibility timeout**: Document and enforce a renewal interval: if the step takes longer than `VisibilityTimeout / 2` to complete, increase `VisibilityTimeout` in the queue config. Or implement auto-renewal in the worker's message-handling loop.
