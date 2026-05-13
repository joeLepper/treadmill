"""TreadmillCloudLite — the dev-local deployment stack (per ADR-0016).

A ``TreadmillCloudLite`` synth/deploy provisions the minimum-AWS-footprint
shape for a Treadmill deployment whose compute (API, Postgres, Redis,
workers) runs on the operator's laptop. Composes:

- :class:`MessagingConstruct`       — SNS events topic + SQS queues
- :class:`DeployEventsConstruct`    — SQS deploy-events queue subscribed to
  events topic with filter policy (entity_type=github AND action=pr_merged)
- :class:`SecretsConstruct`         — github-webhook-secret + github-pat
  + worker-aws-credentials (empty containers; operator populates via
  ``aws secretsmanager put-secret-value``)
- :class:`WebhookReceiverConstruct` — API Gateway HTTP API + Lambda +
  webhook-inbox SQS queue + DLQ per ADR-0017
- :class:`ObservabilityConstruct`   — CloudWatch billing alarm + SNS
  topic for alarm notifications (operator subscribes email post-deploy)

The CloudFormation stack name is derived from ``deployment_id`` —
``Treadmill<PascalCaseDeploymentId>CloudLite`` (e.g. ``personal`` →
``TreadmillPersonalCloudLite``). Same Python class, distinct CFN stacks
per deployment. Resource names inside the stack carry the deployment
suffix (``treadmill-<deployment_id>-*``). The class itself stays a single
canonical spelling — only the synthesized stack name changes per
deployment.

Every taggable resource in the stack inherits a
``treadmill:deployment_id=<deployment_id>`` tag via ``Tags.of(self)``,
so the operator can run
``aws resourcegroupstaggingapi get-resources --tag-filters
Key=treadmill:deployment_id,Values=<id>`` to discover what the
deployment owns; Cost Explorer also slices on this tag.
"""

from __future__ import annotations

import re

import aws_cdk as cdk
from constructs import Construct

from treadmill_infra.constructs import (
    DeployEventsConstruct,
    MessagingConstruct,
    ObservabilityConstruct,
    SecretsConstruct,
    WebhookReceiverConstruct,
)


# Per ADR-0016 §"Canonical spellings": deployment_id is lowercase
# alphanumeric, 1-30 chars, must start with a letter. The 30-char ceiling
# leaves headroom for the longest resource-name suffix (the FIFO DLQ
# convention plus the ``.fifo`` suffix) under SQS's 80-char limit.
_DEPLOYMENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]{0,29}$")


def _validate_deployment_id(deployment_id: str) -> None:
    """Raise ValueError if ``deployment_id`` doesn't match ADR-0016's regex."""
    if not isinstance(deployment_id, str) or not _DEPLOYMENT_ID_PATTERN.match(
        deployment_id
    ):
        raise ValueError(
            f"invalid deployment_id {deployment_id!r}: must match "
            f"{_DEPLOYMENT_ID_PATTERN.pattern} (lowercase alphanumeric, "
            f"1-30 chars, starts with a letter)"
        )


def _stack_name_for(deployment_id: str) -> str:
    """Compute the CFN stack name from ``deployment_id``.

    ``personal`` → ``TreadmillPersonalCloudLite``. The regex forbids ``_``
    so ``.replace("_", "")`` is defensive only; ``.title()`` does the
    actual work (``"personal".title() == "Personal"``).
    """
    return f"Treadmill{deployment_id.title().replace('_', '')}CloudLite"


class TreadmillCloudLite(cdk.Stack):
    """Per-deployment cloud-lite stack for the dev-local topology.

    Args:
        scope: CDK app or parent stage.
        construct_id: CDK logical id (typically computed by the app
            entrypoint as ``_stack_name_for(deployment_id)``; callers
            generally pass the stack name directly so ``cdk deploy
            <stack-name>`` matches).
        deployment_id: Lowercase alphanumeric slug
            (regex ``^[a-z][a-z0-9]{0,29}$``).
        **kwargs: Forwarded to ``cdk.Stack`` (e.g. ``env``).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_id: str,
        **kwargs,
    ) -> None:
        _validate_deployment_id(deployment_id)
        super().__init__(scope, construct_id, **kwargs)

        self.deployment_id = deployment_id

        # Stack-level tag — every taggable resource synthesized below
        # (queues, topics, future Lambda + API Gateway + secrets) inherits
        # this through CDK's tag aspect. ADR-0016 §"Cost attribution
        # backstop" makes this the regression net behind the per-account
        # isolation claim.
        cdk.Tags.of(self).add("treadmill:deployment_id", deployment_id)

        # ── Constructs ────────────────────────────────────────────────────────
        self.messaging = MessagingConstruct(
            self, "Messaging", deployment_id=deployment_id,
        )
        self.deploy_events = DeployEventsConstruct(
            self,
            "DeployEvents",
            deployment_id=deployment_id,
            events_topic=self.messaging.events_topic,
        )
        self.secrets = SecretsConstruct(
            self, "Secrets", deployment_id=deployment_id,
        )
        self.webhook_receiver = WebhookReceiverConstruct(
            self, "WebhookReceiver", deployment_id=deployment_id,
        )
        self.observability = ObservabilityConstruct(
            self, "Observability", deployment_id=deployment_id,
        )
