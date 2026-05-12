"""Messaging construct — SNS events topic + SQS queues for a Treadmill deployment.

Resources provisioned (per ADR-0011 + ADR-0016):

- ``treadmill-<deployment_id>-work.fifo``           — FIFO work queue (API → worker)
- ``treadmill-<deployment_id>-work-dlq.fifo``       — FIFO DLQ for the work queue
- ``treadmill-<deployment_id>-events``              — SNS events topic
- ``treadmill-<deployment_id>-coordination``        — SQS coordination queue subscribed to events
- ``treadmill-<deployment_id>-coordination-dlq``    — DLQ for the coordination queue

The construct exposes the underlying SQS / SNS objects as attributes so
the parent stack (or other constructs that need to grant IAM access, e.g.
the webhook receiver Lambda) can reference them without re-deriving names.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_sqs as sqs,
)
from constructs import Construct


class MessagingConstruct(Construct):
    """SNS topic + SQS queues for a single Treadmill deployment.

    Args:
        scope: Parent CDK scope (typically the ``TreadmillCloudLite`` stack).
        construct_id: CDK logical-id namespace for this construct.
        deployment_id: Lowercase alphanumeric slug (regex ``^[a-z][a-z0-9]{0,29}$``)
            that suffixes every physical resource name.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_id: str,
    ) -> None:
        super().__init__(scope, construct_id)

        prefix = f"treadmill-{deployment_id}"

        # ── Work queue + DLQ ──────────────────────────────────────────────────
        # FIFO source ⇒ FIFO DLQ with content-based dedup. Workers fail fast
        # on poison claims (``runner._receive_one`` self-deletes malformed
        # payloads); max_receive_count=3 is defense-in-depth (decision #11,
        # 2026-05-11 closure plan).
        self.work_dlq = sqs.Queue(
            self,
            "WorkDlq",
            queue_name=f"{prefix}-work-dlq.fifo",
            fifo=True,
            content_based_deduplication=True,
            retention_period=Duration.days(14),
        )

        self.work_queue = sqs.Queue(
            self,
            "WorkQueue",
            queue_name=f"{prefix}-work.fifo",
            fifo=True,
            content_based_deduplication=True,
            visibility_timeout=Duration.seconds(60),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3, queue=self.work_dlq,
            ),
        )

        # ── Events topic + coordination queue + DLQ ───────────────────────────
        # Non-FIFO at v0; consumers (rule engine, observability taps) attach
        # filtered SQS subscriptions when they exist. A future ADR may move
        # to per-entity-type FIFO topics if ordering becomes load-bearing.
        self.events_topic = sns.Topic(
            self,
            "EventsTopic",
            topic_name=f"{prefix}-events",
        )

        # Coordination DLQ — max_receive_count=5 on the source buys ~5 min of
        # retries against a 60s visibility timeout before declaring poison.
        self.events_dlq = sqs.Queue(
            self,
            "EventsDlq",
            queue_name=f"{prefix}-coordination-dlq",
            retention_period=Duration.days(14),
        )

        # Coordination queue — subscribed with raw message delivery so the
        # consumer parses JSON event bodies directly instead of unwrapping
        # SNS envelopes.
        self.events_queue = sqs.Queue(
            self,
            "EventsQueue",
            queue_name=f"{prefix}-coordination",
            visibility_timeout=Duration.seconds(60),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5, queue=self.events_dlq,
            ),
        )
        self.events_topic.add_subscription(
            subscriptions.SqsSubscription(
                self.events_queue, raw_message_delivery=True,
            )
        )

        # ── CloudFormation outputs ────────────────────────────────────────────
        # Consumed by ``treadmill-local init`` to write
        # ``~/.treadmill/<deployment_id>.yaml`` per ADR-0016 §schema. The
        # output keys match the contract the init command reads by suffix
        # (CDK appends an 8-char hash to the logical id).
        cdk.CfnOutput(
            self,
            "EventsTopicArn",
            value=self.events_topic.topic_arn,
            description="ARN of the SNS events topic.",
        )
        cdk.CfnOutput(
            self,
            "EventsQueueUrl",
            value=self.events_queue.queue_url,
            description="URL of the SQS coordination queue subscribed to the events topic.",
        )
        cdk.CfnOutput(
            self,
            "WorkQueueUrl",
            value=self.work_queue.queue_url,
            description="URL of the SQS FIFO work queue.",
        )
