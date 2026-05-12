"""Secrets construct — deployment-scoped Secrets Manager entries.

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

All three secrets are created **without an operator-supplied value**.
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
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct


class SecretsConstruct(Construct):
    """Secrets Manager entries for a single Treadmill deployment.

    Args:
        scope: Parent CDK scope (typically the ``TreadmillCloudLite`` stack).
        construct_id: CDK logical-id namespace for this construct.
        deployment_id: Lowercase alphanumeric slug (regex
            ``^[a-z][a-z0-9]{0,29}$``) that prefixes every secret name.

    Attributes:
        github_webhook_secret: The GitHub-webhook HMAC secret.
        github_pat_secret: The GitHub PAT.
        worker_aws_credentials_secret: The worker IAM-User access keys.
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
        # ``github_pat_secret_name``, ``worker_aws_credentials_secret_name``).
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
