"""Poison-message DLQ behavioral test (closure plan C.8 / decision #11).

Confirms that a message whose payload is structurally valid (parses
cleanly through the typed event registry) but whose handler keeps
raising on every redelivery — concretely: a ``step.completed`` whose
``step_id`` is a syntactically valid UUID that refers to NO row in
``workflow_run_steps``. The Event-row INSERT carries a FK from
``events.step_id → workflow_run_steps.id``; PostgreSQL fails the INSERT
at COMMIT time, which propagates up to ``CoordinationConsumer._process``'s
outer ``except Exception`` and **leaves the message for SQS retry**
(unlike validation failures, which delete the message and never re-enter
the queue).

After ``max_receive_count=5`` deliveries to the source coordination
queue, SQS forwards the un-deleted message to the DLQ
``treadmill-events-coordination-dlq``. That's what this test asserts.

**Why this test is slow.** SQS redrive is timer-driven: each redelivery
costs one ``visibility_timeout`` (60s in the spike CDK), so the floor
for a poison-message landing on the DLQ is
``max_receive_count × visibility_timeout ≈ 5 min``. We budget 7 minutes
with a generous tail so transient consumer-side backoff doesn't cause a
flake. **Run this test sparingly.** It is gated on both
``TREADMILL_INTEGRATION=1`` and ``TREADMILL_DLQ_SMOKE=1`` so a casual
``TREADMILL_INTEGRATION=1 pytest`` run does not stall a contributor for
5+ minutes.

Pre-conditions:
  * ``treadmill-local up`` has been run AFTER the C.2 closure-plan CDK
    changes landed (so the DLQ resources actually exist in moto). A
    substrate brought up against an older CDK won't have the DLQ + this
    test fails with a queue-not-found error early. The first ``getter``
    asserts the DLQ exists and skips the slow path if it doesn't.
  * The API process is running the coordination consumer against the
    same moto endpoint (the default ``treadmill-local up`` topology).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator

import boto3
import pytest

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
DLQ_SMOKE = os.environ.get("TREADMILL_DLQ_SMOKE") == "1"

pytestmark = pytest.mark.skipif(
    not (INTEGRATION and DLQ_SMOKE),
    reason=(
        "set TREADMILL_INTEGRATION=1 AND TREADMILL_DLQ_SMOKE=1 to run; "
        "requires `treadmill-local up`. Slow: ~5–7 min wall-clock per run."
    ),
)


DEFAULT_AWS_ENDPOINT = "http://localhost:5001"


@pytest.fixture(scope="module")
def aws_endpoint_url() -> str:
    return os.environ.get("AWS_ENDPOINT_URL", DEFAULT_AWS_ENDPOINT)


@pytest.fixture(scope="module")
def boto_kwargs(aws_endpoint_url: str) -> dict:
    return dict(
        endpoint_url=aws_endpoint_url,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture
def coordination_queue_url(boto_kwargs: dict) -> str:
    sqs = boto3.client("sqs", **boto_kwargs)
    urls = sqs.list_queues().get("QueueUrls", [])
    matches = [u for u in urls if u.endswith("/treadmill-events-coordination")]
    if not matches:
        pytest.skip(
            "treadmill-events-coordination queue not found in moto — "
            "is `treadmill-local up` running?"
        )
    return matches[0]


@pytest.fixture
def dlq_url(boto_kwargs: dict) -> str:
    sqs = boto3.client("sqs", **boto_kwargs)
    urls = sqs.list_queues().get("QueueUrls", [])
    matches = [u for u in urls if u.endswith("/treadmill-events-coordination-dlq")]
    if not matches:
        pytest.skip(
            "treadmill-events-coordination-dlq not found in moto — "
            "substrate predates the C.2 closure-plan CDK changes; "
            "bring it back up to provision the DLQ."
        )
    return matches[0]


@pytest.fixture
def drain_dlq(boto_kwargs: dict, dlq_url: str) -> Iterator[None]:
    """Drain the DLQ before + after the test so a previous run's poison
    message does not produce a false positive (or leak into the next)."""
    sqs = boto3.client("sqs", **boto_kwargs)

    def _drain() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            resp = sqs.receive_message(
                QueueUrl=dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
            )
            msgs = resp.get("Messages", [])
            if not msgs:
                return
            for m in msgs:
                sqs.delete_message(
                    QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"],
                )

    _drain()
    yield
    _drain()


def test_poison_step_completed_lands_on_dlq_after_max_receive_count(
    boto_kwargs: dict,
    coordination_queue_url: str,
    dlq_url: str,
    drain_dlq: None,
) -> None:
    """A step.completed event whose step_id is a valid UUID but does not
    map to any workflow_run_steps row is the canonical poison case:

      * payload validates cleanly through ``parse_payload`` (the typed
        Pydantic model has no idea whether the UUID resolves);
      * ``_dispatch_step`` issues an UPDATE...WHERE that matches zero
        rows — no error;
      * ``_persist_event`` INSERTs the audit row with the same step_id
        FK; PostgreSQL fails the INSERT with a foreign-key violation;
      * the exception propagates out of ``handle()`` and ``_process``
        leaves the message for retry.

    After ``max_receive_count=5`` failed receives, SQS moves the message
    to the DLQ — which is what this test waits for.

    Wall-clock budget: ~5 min minimum (5 × 60s visibility) plus a
    generous tail for consumer-side backoff. Gated behind
    ``TREADMILL_DLQ_SMOKE=1`` so it does not stall CI.
    """
    sqs = boto3.client("sqs", **boto_kwargs)

    # Sentinel that lets us identify our own message in the DLQ even if
    # other test traffic is in flight. The consumer never reads this
    # field — it's purely a marker for us.
    marker = f"dlq-smoke-{uuid.uuid4()}"

    poison_record = {
        "event_id": str(uuid.uuid4()),
        "entity_type": "step",
        "action": "completed",
        # This UUID is syntactically valid but refers to no real step row.
        # The FK on events.step_id → workflow_run_steps.id fails on
        # INSERT, which is what makes this a poison message (vs a
        # validation failure, which would be deleted on first receive).
        "step_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-08T10:00:00+00:00",
            # Envelope (ADR-0012) validates cleanly — the poison is the
            # unresolvable step_id, not the body shape.
            "output": {
                "summary": "ok",
                "decision": "pushed",
                "artifacts": [{"kind": "branch", "value": "task/poison"}],
            },
        },
        "_dlq_smoke_marker": marker,
    }

    sqs.send_message(
        QueueUrl=coordination_queue_url,
        MessageBody=json.dumps(poison_record),
    )

    # Wait until the message lands on the DLQ. Expected lower bound is
    # ``max_receive_count × visibility_timeout = 5 × 60s = 300s``. We
    # budget 7 minutes so a single slow tick on the consumer side does
    # not flake the assertion.
    visibility_timeout_seconds = 60
    max_receive_count = 5
    minimum_wait = visibility_timeout_seconds * max_receive_count
    deadline = time.monotonic() + minimum_wait + 120  # +2 min tail
    poll_interval = 10

    found = False
    while time.monotonic() < deadline:
        resp = sqs.receive_message(
            QueueUrl=dlq_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=poll_interval,
        )
        for msg in resp.get("Messages", []):
            try:
                body = json.loads(msg["Body"])
            except Exception:
                # Not our message; leave it for other consumers (the DLQ
                # is shared infrastructure). Delete only confirmed-ours
                # messages so a parallel test's poison still surfaces.
                continue
            if body.get("_dlq_smoke_marker") == marker:
                # Found it — clean up and assert success.
                sqs.delete_message(
                    QueueUrl=dlq_url, ReceiptHandle=msg["ReceiptHandle"],
                )
                found = True
                break
        if found:
            break

    assert found, (
        f"poison message did not land on DLQ within "
        f"{minimum_wait + 120}s; check the coordination consumer is "
        f"running and that the source queue has the expected RedrivePolicy "
        f"(maxReceiveCount=5, deadLetterTargetArn pointing at "
        f"{dlq_url})."
    )
