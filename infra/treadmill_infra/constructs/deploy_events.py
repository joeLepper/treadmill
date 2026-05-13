"""Deploy-events messaging construct — SQS queue subscribed to deploy events from SNS.

Resources provisioned per the task spec:

- ``treadmill-<deployment_id>-deploy-events``     — Standard SQS queue subscribed to
  events topic with filter policy for entity_type=github AND action=pr_merged
- ``treadmill-<deployment_id>-deploy-events-dlq`` — DLQ for the deploy-events queue
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

# ``SubscriptionFilter`` lives in ``aws_cdk.aws_sns``, not in
# ``aws_cdk.aws_sns_subscriptions``. wf-author hallucinated the import
# location; correcting here as part of the manual-cleanup pass.


class DeployEventsConstruct(Construct):
    """SQS deploy-events queue subscribed to filtered SNS events topic.

    Args:
        scope: Parent CDK scope (typically the ``TreadmillCloudLite`` stack).
        construct_id: CDK logical-id namespace for this construct.
        deployment_id: Lowercase alphanumeric slug (regex ``^[a-z][a-z0-9]{0,29}$``)
            that suffixes every physical resource name.
        events_topic: The SNS events topic to subscribe to.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_id: str,
        events_topic: sns.Topic,
    ) -> None:
        super().__init__(scope, construct_id)

        prefix = f"treadmill-{deployment_id}"

        # ── Deploy-events DLQ ─────────────────────────────────────────────────
        self.dlq = sqs.Queue(
            self,
            "DeployEventsDlq",
            queue_name=f"{prefix}-deploy-events-dlq",
            retention_period=Duration.days(14),
        )

        # ── Deploy-events queue ────────────────────────────────────────────────
        # Standard (not FIFO) queue subscribed to events topic with filter
        # policy limiting to github PRs merged. Visibility timeout 30s covers
        # cached-image rebuilds; longer rebuilds will re-deliver and be deduped
        # by the watcher's state file. Max receive count 3 before routing to DLQ.
        self.queue = sqs.Queue(
            self,
            "DeployEventsQueue",
            queue_name=f"{prefix}-deploy-events",
            visibility_timeout=Duration.seconds(30),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3, queue=self.dlq,
            ),
            retention_period=Duration.days(14),
        )

        # ── SNS subscription with filter policy ──────────────────────────────
        events_topic.add_subscription(
            subscriptions.SqsSubscription(
                self.queue,
                filter_policy={
                    "entity_type": sns.SubscriptionFilter.string_filter(
                        allowlist=["github"],
                    ),
                    "action": sns.SubscriptionFilter.string_filter(
                        allowlist=["pr_merged"],
                    ),
                },
            )
        )

        # ── CloudFormation outputs ─────────────────────────────────────────────
        cdk.CfnOutput(
            self,
            "DeployEventsQueueUrl",
            value=self.queue.queue_url,
            description="URL of the SQS deploy-events queue.",
        )
        cdk.CfnOutput(
            self,
            "DeployEventsDlqUrl",
            value=self.dlq.queue_url,
            description="URL of the deploy-events DLQ.",
        )
