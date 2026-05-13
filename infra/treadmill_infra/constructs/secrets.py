"""Secrets construct — deployment-scoped Secrets Manager entries + API IAM user.

Provisions three Secrets Manager secrets for a Treadmill dev-local
deployment (per ADR-0016 + Week-4 plan B.2):

- ``treadmill-<deployment_id>/github-webhook-secret`` — GitHub webhook
  HMAC verification secret (consumed by the webhook-inbox poller per
  ADR-0017).
- ``treadmill-<deployment_id>/github-pat`` — GitHub Personal Access
  Token (read by the worker at startup; piped into
  ``gh auth login --with-token`` per ADR-0016 Q16.d).
- ``treadmill-<deployment_id>/worker-aws-credentials`` — long-lived
  IAM-User access key pair for the worker's boto3 clients
  (per ADR-0016 §"Long-lived IAM-User keys per deployment").
- ``treadmill-<deployment_id>/api-aws-credentials`` — long-lived
  IAM-User access key pair for the API's boto3 clients (per ADR-0023).

All four secrets are created **without an operator-supplied value**.
The operator populates them post-deploy via
``aws secretsmanager put-secret-value`` (see the cold-start walkthrough
in Phase E.1 of the Week-4 plan). The CDK ``Secret`` constructor with no
``secret_string_value`` / ``secret_object_value`` / ``generate_secret_string``
arg still emits a default ``GenerateSecretString: {}`` block in the
CloudFormation template, which produces a throwaway random placeholder
at stack-create time; that placeholder is then overwritten by the
operator's first ``put-secret-value``. This is the canonical pattern for
"create the secret resource now, fill in the value later" and keeps the
real secret values out of the synthesized CFN template entirely.

**Removal-policy contract.** Every secret here is created with
``removal_policy=cdk.RemovalPolicy.DESTROY`` so CloudFormation issues a
delete call on stack destroy. **Force-delete-without-recovery is NOT a
CloudFormation property** — it's a parameter on the
``DeleteSecret`` API call only. There is no way to express "skip the
7-30 day recovery window" through ``AWS::SecretsManager::Secret`` at
create or delete time via CloudFormation. The operator's pre-``cdk
destroy`` runbook (see ``docs/plans/2026-05-13-...md`` §"Stack deletion +
redeploy") covers it explicitly via
``aws secretsmanager delete-secret --force-delete-without-recovery``
before ``cdk destroy`` runs. This was the runbook's job all along; the
prior B.2 attempt to express it as a CFN property fails at deploy time
("Unsupported property [ForceDeleteWithoutRecovery]") and was removed.

**API IAM user (per ADR-0023).** The construct also provisions a long-lived
IAM user ``treadmill-<deployment_id>-api`` with an inline policy granting
access to the SQS coordination/webhook-inbox/work queues, SNS events topic,
and Secrets Manager github-webhook-secret. The operator generates access
keys manually post-deploy and populates ``api-aws-credentials``.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct


class SecretsConstruct(Construct):
    """Secrets Manager entries + API IAM user for a single Treadmill deployment.

    Args:
        scope: Parent CDK scope (typically the ``TreadmillCloudLite`` stack).
        construct_id: CDK logical-id namespace for this construct.
        deployment_id: Lowercase alphanumeric slug (regex
            ``^[a-z][a-z0-9]{0,29}$``) that prefixes every secret name.

    Attributes:
        github_webhook_secret: The GitHub-webhook HMAC secret.
        github_pat_secret: The GitHub PAT.
        worker_aws_credentials_secret: The worker IAM-User access keys.
        api_aws_credentials_secret: The API IAM-User access keys.
        api_user: The API IAM user (treadmill-<deployment_id>-api).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_id: str,
    ) -> None:
        super().__init__(scope, construct_id)

        self.deployment_id = deployment_id
        prefix = f"treadmill-{deployment_id}"

        github_webhook_secret_name = f"{prefix}/github-webhook-secret"
        github_pat_secret_name = f"{prefix}/github-pat"
        worker_aws_credentials_secret_name = (
            f"{prefix}/worker-aws-credentials"
        )

        # ── GitHub webhook HMAC secret ────────────────────────────────────────
        self.github_webhook_secret = self._make_secret(
            "GithubWebhookSecret",
            secret_name=github_webhook_secret_name,
            description=(
                "GitHub webhook HMAC verification secret for "
                f"deployment {deployment_id}. Populate via "
                "`aws secretsmanager put-secret-value`."
            ),
        )

        # ── GitHub PAT ────────────────────────────────────────────────────────
        self.github_pat_secret = self._make_secret(
            "GithubPatSecret",
            secret_name=github_pat_secret_name,
            description=(
                "GitHub Personal Access Token for "
                f"deployment {deployment_id}. Populate via "
                "`aws secretsmanager put-secret-value`."
            ),
        )

        # ── Worker AWS credentials (long-lived IAM-User access key pair) ─────
        self.worker_aws_credentials_secret = self._make_secret(
            "WorkerAwsCredentialsSecret",
            secret_name=worker_aws_credentials_secret_name,
            description=(
                "Long-lived IAM-User access key pair for the "
                f"{deployment_id} worker. Populate via "
                "`aws secretsmanager put-secret-value`."
            ),
        )

        # ── API IAM user + AWS credentials ────────────────────────────────────
        # Per ADR-0023: the API gets a long-lived IAM user with a narrowly
        # scoped policy granting only the actions it actually performs.
        api_iam_user_name = f"{prefix}-api"
        self.api_user = iam.User(
            self,
            "ApiUser",
            user_name=api_iam_user_name,
        )

        # Inline policy per ADR-0023 §"IAM scope": seven actions across four
        # resource scopes, expressed as four PolicyStatements grouped by action
        # class. The per-statement grouping is deliberate — a single combined
        # statement would have to apply ``sqs:SendMessage`` to the coordination
        # + webhook-inbox queues (overpermissioning) or ``sqs:ReceiveMessage`` to
        # the work-queue (also overpermissioning). Four statements keep each
        # action set scoped to the resource ARN(s) that should actually accept it.
        sqs_coordination_arn = self._queue_arn(deployment_id, "coordination")
        sqs_webhook_inbox_arn = self._queue_arn(deployment_id, "webhook-inbox")
        sqs_work_fifo_arn = self._queue_arn(deployment_id, "work.fifo")
        sns_events_arn = self._topic_arn(deployment_id, "events")
        secrets_github_webhook_arn = self._secret_arn(
            deployment_id, "github-webhook-secret"
        )

        api_policy = iam.Policy(
            self,
            "ApiUserPolicy",
            statements=[
                iam.PolicyStatement(
                    actions=[
                        "sqs:ReceiveMessage",
                        "sqs:DeleteMessage",
                        "sqs:ChangeMessageVisibility",
                        "sqs:GetQueueAttributes",
                        "sqs:GetQueueUrl",
                    ],
                    resources=[sqs_coordination_arn, sqs_webhook_inbox_arn],
                ),
                iam.PolicyStatement(
                    actions=["sqs:SendMessage"],
                    resources=[sqs_work_fifo_arn],
                ),
                iam.PolicyStatement(
                    actions=["sns:Publish"],
                    resources=[sns_events_arn],
                ),
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[secrets_github_webhook_arn],
                ),
            ],
        )
        api_policy.attach_to_user(self.api_user)

        # API AWS credentials secret (empty; operator populates via put-secret-value)
        api_aws_credentials_secret_name = f"{prefix}/api-aws-credentials"
        self.api_aws_credentials_secret = self._make_secret(
            "ApiAwsCredentialsSecret",
            secret_name=api_aws_credentials_secret_name,
            description=(
                "Long-lived IAM-User access key pair for the "
                f"{deployment_id} API. Populate via "
                "`aws secretsmanager put-secret-value`."
            ),
        )

        # ── CloudFormation outputs ────────────────────────────────────────────
        # The output values are the deterministic, operator-facing secret
        # names (the same strings we passed as ``secret_name``) — NOT
        # ``secret.secret_name``, which is a token that synthesizes into a
        # complex ``Fn::Join`` of ``Fn::Split`` expressions that
        # reconstruct the name from the ARN at deploy time. Since we
        # explicitly set the name and need ``treadmill-local init`` to read
        # an operator-readable literal from the CFN output, the literal
        # form is the right choice. ADR-0016's YAML schema names these
        # output keys (matching the YAML keys ``github_webhook_secret_name``,
        # ``github_pat_secret_name``, ``worker_aws_credentials_secret_name``,
        # ``api_aws_credentials_secret_name``).
        cdk.CfnOutput(
            self,
            "GithubWebhookSecretName",
            value=github_webhook_secret_name,
            description="Name of the GitHub-webhook HMAC secret.",
        )
        cdk.CfnOutput(
            self,
            "GithubPatSecretName",
            value=github_pat_secret_name,
            description="Name of the GitHub PAT secret.",
        )
        cdk.CfnOutput(
            self,
            "WorkerAwsCredentialsSecretName",
            value=worker_aws_credentials_secret_name,
            description="Name of the worker AWS credentials secret.",
        )
        cdk.CfnOutput(
            self,
            "ApiIamUserArn",
            value=self.api_user.user_arn,
            description="ARN of the API IAM user.",
        )
        cdk.CfnOutput(
            self,
            "ApiAwsCredentialsSecretName",
            value=api_aws_credentials_secret_name,
            description="Name of the API AWS credentials secret.",
        )

    def _make_secret(
        self,
        construct_id: str,
        *,
        secret_name: str,
        description: str,
    ) -> secretsmanager.Secret:
        """Create a ``Secret`` with the DESTROY removal policy.

        ``removal_policy=DESTROY`` so CloudFormation issues a delete call
        on stack destroy. Skipping Secrets Manager's 7-30 day recovery
        window is a separate operator step run *before* ``cdk destroy``
        (see the stack-deletion runbook in the Week-4 plan).
        """
        return secretsmanager.Secret(
            self,
            construct_id,
            secret_name=secret_name,
            description=description,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

    def _queue_arn(self, deployment_id: str, queue_name: str) -> str:
        """Construct SQS queue ARN."""
        account_id = cdk.Stack.of(self).account
        region = cdk.Stack.of(self).region
        full_queue_name = f"treadmill-{deployment_id}-{queue_name}"
        return f"arn:aws:sqs:{region}:{account_id}:{full_queue_name}"

    def _topic_arn(self, deployment_id: str, topic_name: str) -> str:
        """Construct SNS topic ARN."""
        account_id = cdk.Stack.of(self).account
        region = cdk.Stack.of(self).region
        full_topic_name = f"treadmill-{deployment_id}-{topic_name}"
        return f"arn:aws:sns:{region}:{account_id}:{full_topic_name}"

    def _secret_arn(self, deployment_id: str, secret_suffix: str) -> str:
        """Construct Secrets Manager secret ARN."""
        account_id = cdk.Stack.of(self).account
        region = cdk.Stack.of(self).region
        full_secret_name = f"treadmill-{deployment_id}/{secret_suffix}"
        # Secrets Manager ARNs are arn:aws:secretsmanager:region:account-id:secret:name-XXXXXX
        # where -XXXXXX is a 6-char random suffix added by AWS.
        return f"arn:aws:secretsmanager:{region}:{account_id}:secret:{full_secret_name}-*"
