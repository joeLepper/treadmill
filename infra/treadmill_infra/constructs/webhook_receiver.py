"""Webhook receiver construct — API Gateway HTTP API + Lambda + SQS inbox.

Per ADR-0017 §"CDK resources (part of ``TreadmillCloudLite``)" + §"The Lambda"
+ §"Header preservation contract". Provisions the AWS-side webhook ingress
path that GitHub POSTs into and that the local API's webhook-inbox poller
drains:

- ``treadmill-<deployment_id>-webhook-inbox``     — SQS standard queue (60s visibility, 14d retention)
- ``treadmill-<deployment_id>-webhook-inbox-dlq`` — SQS DLQ (14d retention), wired with ``maxReceiveCount=5``
- ``treadmill-<deployment_id>-webhook-receiver``  — Lambda function (Python 3.12)
- ``treadmill-<deployment_id>-webhook-api``       — API Gateway HTTP API with ``POST /webhook/github``

The Lambda receives the API Gateway event, builds the ``{headers, body}``
envelope per ADR-0017's "Header preservation contract" (preserving
``x-github-event``, ``x-github-delivery``, ``x-hub-signature-256``), and
calls ``sqs.send_message`` to push the envelope onto the inbox queue. The
local poller validates the envelope, verifies the HMAC signature, persists
the Event row, and publishes to SNS.

Signature verification deliberately lives in the local poller, not the
Lambda — keeping the AWS-side cost surface tiny and signature crypto in
the existing well-tested module (``webhooks/signatures.py``).

CloudFormation outputs (consumed by ``treadmill-local init`` writing to
``~/.treadmill/<deployment_id>.yaml``):

- ``WebhookApiUrl``         — public HTTPS URL of the API Gateway endpoint
- ``WebhookInboxQueueUrl``  — URL of the SQS webhook-inbox queue
- ``WebhookInboxDlqUrl``    — URL of the SQS webhook-inbox DLQ

The construct is factored so adding ``POST /webhook/slack`` (or any other
``/webhook/<source>``) later is a single-line change: register one more
entry in the route table dict. v0 ships one entry.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_apigatewayv2 as apigwv2,
    aws_lambda as lambda_,
    aws_sqs as sqs,
)
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct


# Path (relative to the CDK app's working directory — ``infra/``) to the
# Lambda asset directory. Phase B.3 populates ``handler.py`` with the real
# wrap-and-enqueue implementation; Phase B.1 just needs the directory to
# exist so ``Code.from_asset`` resolves at ``cdk synth`` time.
_LAMBDA_ASSET_PATH = "lambdas/webhook_receiver"


class WebhookReceiverConstruct(Construct):
    """API Gateway HTTP API + Lambda + SQS inbox for GitHub webhook ingress.

    Args:
        scope: Parent CDK scope (typically the ``TreadmillCloudLite`` stack).
        construct_id: CDK logical-id namespace for this construct.
        deployment_id: Lowercase alphanumeric slug (regex
            ``^[a-z][a-z0-9]{0,29}$``) that suffixes every physical
            resource name.
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

        # ── SQS webhook-inbox DLQ ─────────────────────────────────────────────
        # The DLQ catches messages the poller cannot process after
        # ``maxReceiveCount=5`` attempts. The DLQ URL is exported as a CFN
        # output so the operator runbook ("the DLQ has messages — what
        # now?") can ``aws sqs receive-message`` against it directly.
        self.webhook_inbox_dlq = sqs.Queue(
            self,
            "WebhookInboxDlq",
            queue_name=f"{prefix}-webhook-inbox-dlq",
            retention_period=Duration.days(14),
        )

        # ── SQS webhook-inbox queue ───────────────────────────────────────────
        # Standard (not FIFO) — GitHub webhooks have no ordering guarantees.
        # Visibility timeout 60s per ADR-0017 §"CDK resources": steady-state
        # processing is ~250ms (Secrets Manager cache + HMAC verify + DB
        # INSERT + SNS publish), so 60s comfortably absorbs cold starts.
        self.webhook_inbox_queue = sqs.Queue(
            self,
            "WebhookInboxQueue",
            queue_name=f"{prefix}-webhook-inbox",
            visibility_timeout=Duration.seconds(60),
            retention_period=Duration.days(14),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5, queue=self.webhook_inbox_dlq,
            ),
        )

        # ── Lambda function ───────────────────────────────────────────────────
        # The handler reads ``event['headers']`` + ``event['body']``, wraps
        # them in a JSON envelope, and writes to SQS. It does NOT verify
        # signatures (that's the local poller's job per ADR-0017).
        #
        # CloudWatch Logs: CDK attaches the AWS-managed
        # ``AWSLambdaBasicExecutionRole`` policy by default for every
        # ``lambda_.Function``, which grants ``logs:CreateLogGroup``,
        # ``logs:CreateLogStream``, and ``logs:PutLogEvents``. We do NOT
        # override the default role, so logging is on — note this so
        # reviewers don't think logging is absent because no explicit grant
        # appears below.
        self.webhook_lambda = lambda_.Function(
            self,
            "WebhookLambda",
            function_name=f"{prefix}-webhook-receiver",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(_LAMBDA_ASSET_PATH),
            environment={
                # Single canonical spelling shared by the Lambda's
                # ``os.environ["WEBHOOK_INBOX_QUEUE_URL"]`` lookup.
                "WEBHOOK_INBOX_QUEUE_URL": self.webhook_inbox_queue.queue_url,
            },
        )

        # IAM: exactly one grant — ``sqs:SendMessage`` on the inbox queue.
        # No other AWS calls; the assertion test in
        # ``test_webhook_receiver_construct.py`` is the regression net.
        self.webhook_inbox_queue.grant_send_messages(self.webhook_lambda)

        # ── API Gateway HTTP API ──────────────────────────────────────────────
        # Single route at v0: ``POST /webhook/github``. The route table is
        # a dict so adding ``/webhook/slack`` later is one line. The
        # ``api_name`` parameter sets a human-readable name (visible in
        # the AWS console) — the auto-generated URL is what GitHub points
        # at, surfaced as the ``WebhookApiUrl`` CFN output below.
        self.http_api = apigwv2.HttpApi(
            self,
            "WebhookHttpApi",
            api_name=f"{prefix}-webhook-api",
        )

        # Route table: path → Lambda. v0 ships one entry; future
        # ``/webhook/slack`` etc. are additive without restructuring.
        routes: dict[str, lambda_.Function] = {
            "/webhook/github": self.webhook_lambda,
        }
        for path, fn in routes.items():
            self.http_api.add_routes(
                path=path,
                methods=[apigwv2.HttpMethod.POST],
                integration=HttpLambdaIntegration(
                    f"WebhookIntegration{path.replace('/', '_')}",
                    fn,
                ),
            )

        # ── CloudFormation outputs ────────────────────────────────────────────
        # Consumed by ``treadmill-local init`` to write
        # ``~/.treadmill/<deployment_id>.yaml`` per ADR-0016 §schema.
        cdk.CfnOutput(
            self,
            "WebhookApiUrl",
            value=self.http_api.api_endpoint,
            description="Base URL of the webhook receiver API Gateway HTTP API.",
        )
        cdk.CfnOutput(
            self,
            "WebhookInboxQueueUrl",
            value=self.webhook_inbox_queue.queue_url,
            description="URL of the SQS webhook-inbox queue.",
        )
        cdk.CfnOutput(
            self,
            "WebhookInboxDlqUrl",
            value=self.webhook_inbox_dlq.queue_url,
            description="URL of the SQS webhook-inbox dead-letter queue.",
        )
