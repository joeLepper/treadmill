"""TreadmillObservabilityStack — one observability stack per deployment.

Per ADR-0020 §"One observability stack per deployment, deployed identically":
deploys Grafana + Tempo + Loki + Prometheus + OTel Collector as a single
docker-compose unit on one EC2 instance. This stack is a *sibling* to
``TreadmillCloudLite`` (Q20.f decision: separate CDK stack so observability
can deploy and tear down independently of the messaging/queues stack).

Resources created:

- **EC2 instance** (t3.small default; configurable via ``instance_type``)
  with the SSM-managed instance role (no SSH required; access via
  ``aws ssm start-session``).
- **Two EBS volumes** (100 GB each, GP3): one for Loki chunks, one for
  Prometheus TSDB. Mounted at ``/mnt/loki`` and ``/mnt/prometheus``
  respectively; bind-mounted into the containers by the compose file.
- **S3 bucket** for Tempo trace blob storage. The EC2 role is granted
  read/write access; Tempo uses the instance profile credentials.
- **S3 asset**: ``infra/observability/`` is zipped and uploaded to the CDK
  bootstrap bucket at synthesis time. The user-data script downloads and
  unpacks it on the EC2 at launch.
- **Secrets Manager secret** for the Grafana admin password (randomly
  generated, 32 chars, no punctuation). The user-data script reads it at
  launch and injects it into the compose ``.env`` file.
- **Security group**: no public ingress by default; the operator reaches
  Grafana via ``aws ssm start-session`` port-forwarding. Pass
  ``operator_cidr`` to additionally open ports 3000, 4317, and 4318 to a
  known CIDR (e.g., the operator's office IP).

CloudFormation outputs (read by ``treadmill-local init`` into the
per-deployment YAML):

- ``ObservabilityCollectorEndpoint`` — ``<private-ip>:4317`` (OTLP gRPC)
- ``ObservabilityGrafanaHost``       — private IP of the Grafana EC2
- ``ObservabilityEc2InstanceId``     — for ``aws ssm start-session``
- ``ObservabilityGrafanaAdminSecretArn`` — Secrets Manager ARN for the
  Grafana admin password

Stack naming: ``Treadmill<PascalCaseDeploymentId>Observability``
(e.g., ``personal`` → ``TreadmillPersonalObservability``).
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from treadmill_infra.stacks.cloud_lite import _validate_deployment_id


def _obs_stack_name_for(deployment_id: str) -> str:
    """Compute the CFN stack name for the observability stack.

    ``personal`` → ``TreadmillPersonalObservability``.
    """
    return f"Treadmill{deployment_id.title().replace('_', '')}Observability"


class TreadmillObservabilityStack(cdk.Stack):
    """Per-deployment observability stack: Grafana + Tempo + Loki + Prometheus + OTel.

    Args:
        scope: CDK app or parent stage.
        construct_id: CDK logical id (typically computed by the app entrypoint
            as ``_obs_stack_name_for(deployment_id)``).
        deployment_id: Lowercase alphanumeric slug matching the sibling
            ``TreadmillCloudLite`` deployment (regex ``^[a-z][a-z0-9]{0,29}$``).
        instance_type: EC2 instance type for the observability host. Default
            ``t3.small`` (~$15/month) is sufficient for a single-operator
            dev-local deployment.
        operator_cidr: Optional CIDR block to open Grafana (3000) and OTLP
            (4317, 4318) ports to. When ``None`` (default), the security group
            has no public ingress — access is via SSM session manager only.
        vpc: Optional VPC to launch the instance into. When ``None`` (default),
            the account's default VPC is used via ``Vpc.from_lookup``.
        **kwargs: Forwarded to ``cdk.Stack`` (e.g. ``env``).

    Exposes:
        instance: The ``ec2.Instance`` running the docker-compose stack.
        tempo_bucket: The ``s3.Bucket`` used for Tempo trace storage.
        grafana_secret: The ``secretsmanager.Secret`` holding the Grafana
            admin password.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_id: str,
        instance_type: str = "t3.small",
        operator_cidr: str | None = None,
        vpc: ec2.IVpc | None = None,
        **kwargs,
    ) -> None:
        _validate_deployment_id(deployment_id)
        super().__init__(scope, construct_id, **kwargs)

        self.deployment_id = deployment_id
        prefix = f"treadmill-{deployment_id}"

        # Stack-level tag — every taggable resource inherits for cost
        # attribution (ADR-0016 §"Cost attribution backstop").
        cdk.Tags.of(self).add("treadmill:deployment_id", deployment_id)

        # ── VPC ───────────────────────────────────────────────────────────────
        # Use the provided VPC or look up the account's default VPC. The
        # default VPC is correct for dev-local (single-account, single-region).
        if vpc is None:
            vpc = ec2.Vpc.from_lookup(self, "Vpc", is_default=True)

        # ── Tempo S3 bucket ───────────────────────────────────────────────────
        # Tempo writes trace blobs here. RETAIN on deletion to avoid losing
        # traces if the operator tears down and re-deploys the stack.
        self.tempo_bucket = s3.Bucket(
            self,
            "TempoBucket",
            bucket_name=f"{prefix}-tempo-traces",
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=cdk.Duration.days(30),
                    id="expire-old-traces",
                ),
            ],
        )

        # ── Grafana admin password (Secrets Manager) ──────────────────────────
        # Generated once at deploy time; never stored in the compose file or
        # git. The user-data script reads it via the EC2 instance role.
        self.grafana_secret = secretsmanager.Secret(
            self,
            "GrafanaAdminSecret",
            secret_name=f"{prefix}-grafana-admin-password",
            description=f"Grafana admin password for Treadmill {deployment_id}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=32,
                exclude_punctuation=True,
            ),
        )

        # ── IAM role for the EC2 instance ─────────────────────────────────────
        # AmazonSSMManagedInstanceCore enables SSM session manager (no SSH
        # key pair needed). Specific resource grants are added below.
        role = iam.Role(
            self,
            "ObservabilityRole",
            role_name=f"{prefix}-observability-ec2",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )

        # Tempo writes/reads traces from S3.
        self.tempo_bucket.grant_read_write(role)

        # User-data reads the Grafana admin password at launch.
        self.grafana_secret.grant_read(role)

        # ── S3 asset: infra/observability/ ────────────────────────────────────
        # CDK zips the directory at synthesis time and uploads it to the
        # bootstrap bucket. The user-data script downloads + unpacks it.
        observability_dir = Path(__file__).parent.parent.parent.parent / "observability"
        asset = s3_assets.Asset(
            self,
            "ObservabilityAsset",
            path=str(observability_dir),
        )
        # Grant the instance role read access to the CDK bootstrap bucket.
        asset.grant_read(role)

        # ── Security group ────────────────────────────────────────────────────
        sg = ec2.SecurityGroup(
            self,
            "ObservabilitySg",
            vpc=vpc,
            security_group_name=f"{prefix}-observability",
            description=(
                f"Treadmill {deployment_id} observability EC2. "
                "No public ingress by default; use SSM session manager."
            ),
            allow_all_outbound=True,
        )
        if operator_cidr:
            sg.add_ingress_rule(
                ec2.Peer.ipv4(operator_cidr),
                ec2.Port.tcp(3000),
                "Grafana UI from operator CIDR",
            )
            sg.add_ingress_rule(
                ec2.Peer.ipv4(operator_cidr),
                ec2.Port.tcp(4317),
                "OTLP gRPC from operator CIDR",
            )
            sg.add_ingress_rule(
                ec2.Peer.ipv4(operator_cidr),
                ec2.Port.tcp(4318),
                "OTLP HTTP from operator CIDR",
            )

        # ── User-data script ──────────────────────────────────────────────────
        # Installs Docker + Compose, mounts EBS volumes, downloads the compose
        # asset, injects secrets, and runs ``docker compose up -d``.
        user_data = ec2.UserData.for_linux()
        secret_name = f"{prefix}-grafana-admin-password"
        tempo_bucket_name = f"{prefix}-tempo-traces"
        user_data.add_commands(
            "#!/bin/bash",
            "set -euo pipefail",
            "exec > >(tee /var/log/treadmill-observability-init.log | logger -t treadmill-obs) 2>&1",
            "",
            "# Get region from IMDSv2 (avoids CDK token resolution issues)",
            "TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token "
            "-H 'X-aws-ec2-metadata-token-ttl-seconds: 21600')",
            "REGION=$(curl -s -H \"X-aws-ec2-metadata-token: $TOKEN\" "
            "http://169.254.169.254/latest/meta-data/placement/region)",
            "export AWS_DEFAULT_REGION=$REGION",
            "",
            "# Install Docker",
            "dnf install -y docker unzip",
            "systemctl enable --now docker",
            "",
            "# Install Docker Compose CLI plugin",
            "ARCH=$(uname -m)",
            "mkdir -p /usr/local/lib/docker/cli-plugins",
            "curl -fsSL \"https://github.com/docker/compose/releases/download/v2.27.0/"
            "docker-compose-linux-${ARCH}\" "
            "-o /usr/local/lib/docker/cli-plugins/docker-compose",
            "chmod +x /usr/local/lib/docker/cli-plugins/docker-compose",
            "",
            "# Format and mount EBS volumes (idempotent: skip if already formatted)",
            "mkdir -p /mnt/loki /mnt/prometheus",
            "if ! blkid /dev/xvdb; then mkfs -t xfs /dev/xvdb; fi",
            "if ! blkid /dev/xvdc; then mkfs -t xfs /dev/xvdc; fi",
            "mount /dev/xvdb /mnt/loki",
            "mount /dev/xvdc /mnt/prometheus",
            "grep -q '/dev/xvdb' /etc/fstab || "
            "echo '/dev/xvdb /mnt/loki xfs defaults 0 2' >> /etc/fstab",
            "grep -q '/dev/xvdc' /etc/fstab || "
            "echo '/dev/xvdc /mnt/prometheus xfs defaults 0 2' >> /etc/fstab",
            "",
            "# Download and unpack the observability compose asset from S3",
            "mkdir -p /opt/treadmill-observability",
            f"aws s3 cp s3://{asset.s3_bucket_name}/{asset.s3_object_key} "
            "/tmp/observability.zip",
            "cd /opt/treadmill-observability && unzip -o /tmp/observability.zip",
            "",
            "# Fetch the Grafana admin password from Secrets Manager",
            "GRAFANA_PASS=$(aws secretsmanager get-secret-value "
            f"--secret-id {secret_name} "
            "--query SecretString --output text)",
            "",
            "# Write the docker-compose .env file",
            "cat > /opt/treadmill-observability/.env <<ENVEOF",
            f"TEMPO_S3_BUCKET={tempo_bucket_name}",
            "AWS_DEFAULT_REGION=${REGION}",
            "ENVEOF",
            'echo "GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASS}" '
            ">> /opt/treadmill-observability/.env",
            "",
            "# Start the observability stack",
            "cd /opt/treadmill-observability && docker compose up -d",
        )

        # ── EC2 instance ──────────────────────────────────────────────────────
        # Amazon Linux 2023 (AL2023) uses dnf; the user-data script above
        # targets AL2023. Block devices: root (20 GB) + Loki (100 GB) +
        # Prometheus (100 GB).
        self.instance = ec2.Instance(
            self,
            "ObservabilityInstance",
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=vpc,
            role=role,
            security_group=sg,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        20,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                    ),
                ),
                # Loki chunks volume
                ec2.BlockDevice(
                    device_name="/dev/xvdb",
                    volume=ec2.BlockDeviceVolume.ebs(
                        100,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                    ),
                ),
                # Prometheus TSDB volume
                ec2.BlockDevice(
                    device_name="/dev/xvdc",
                    volume=ec2.BlockDeviceVolume.ebs(
                        100,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                    ),
                ),
            ],
            user_data=user_data,
        )

        # ── CloudFormation outputs ────────────────────────────────────────────
        CfnOutput(
            self,
            "ObservabilityCollectorEndpoint",
            value=cdk.Fn.join("", [self.instance.instance_private_ip, ":4317"]),
            description=(
                "OTLP gRPC endpoint for the OTel Collector (host:4317). "
                "Set as OTEL_EXPORTER_OTLP_ENDPOINT in the worker + API env."
            ),
        )
        CfnOutput(
            self,
            "ObservabilityGrafanaHost",
            value=self.instance.instance_private_ip,
            description=(
                "Private IP of the Grafana EC2 instance. "
                "Reach Grafana via: aws ssm start-session ... "
                "AWS-StartPortForwardingSessionToRemoteHost (tunnel :3000)."
            ),
        )
        CfnOutput(
            self,
            "ObservabilityEc2InstanceId",
            value=self.instance.instance_id,
            description=(
                "EC2 instance ID for SSM session forwarding. "
                "Use with: aws ssm start-session --target <id>."
            ),
        )
        CfnOutput(
            self,
            "ObservabilityGrafanaAdminSecretArn",
            value=self.grafana_secret.secret_arn,
            description=(
                "ARN of the Secrets Manager secret holding the Grafana admin "
                "password. Retrieve with: aws secretsmanager get-secret-value "
                f"--secret-id {secret_name}."
            ),
        )
