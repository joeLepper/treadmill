"""MotoProvisioner — translate parsed CFN resources into boto3 calls against moto.

For the spike, we provision:
  - AWS::SNS::Topic (FIFO and standard)
  - AWS::SQS::Queue (FIFO and standard; optional RedrivePolicy + retention)
  - AWS::SNS::Subscription (SQS subscriptions to topics)
  - AWS::S3::Bucket

We deliberately ignore everything else. The runtime is responsible for ECS
task-definition handling and container orchestration.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
from rich.console import Console

from treadmill_local.synth import CFNResource, SynthResult

console = Console()

_FAKE_REGION = "us-east-1"
_FAKE_ACCOUNT = "123456789012"  # moto's default account ID


class MotoProvisioner:
    def __init__(self, endpoint_url: str) -> None:
        self.endpoint_url = endpoint_url
        # boto3 still requires credentials even for moto; any non-empty values work.
        self._kwargs = {
            "endpoint_url": endpoint_url,
            "region_name": _FAKE_REGION,
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
        }
        self.sns = boto3.client("sns", **self._kwargs)
        self.sqs = boto3.client("sqs", **self._kwargs)
        self.s3 = boto3.client("s3", **self._kwargs)
        # Map from CFN logical ID → real ARN/URL once provisioned. Used to
        # resolve `Ref` / `Fn::GetAtt` references in subscription resources.
        self._refs: dict[str, str] = {}

    def provision(self, synth: SynthResult) -> None:
        # Order matters: topics and queues before subscriptions.
        self._provision_topics(synth.by_type("AWS::SNS::Topic"))
        self._provision_queues(synth.by_type("AWS::SQS::Queue"))
        self._provision_buckets(synth.by_type("AWS::S3::Bucket"))
        self._provision_subscriptions(synth.by_type("AWS::SNS::Subscription"))

    # ── SNS ───────────────────────────────────────────────────────────────────

    def _provision_topics(self, resources: list[CFNResource]) -> None:
        for r in resources:
            name = r.properties.get("TopicName") or r.logical_id
            attrs: dict[str, str] = {}
            if r.properties.get("FifoTopic"):
                attrs["FifoTopic"] = "true"
            if r.properties.get("ContentBasedDeduplication"):
                attrs["ContentBasedDeduplication"] = "true"
            resp = self.sns.create_topic(Name=name, Attributes=attrs)
            arn = resp["TopicArn"]
            self._refs[r.logical_id] = arn
            console.print(f"  ✓ SNS topic [cyan]{name}[/cyan] → {arn}")

    # ── SQS ───────────────────────────────────────────────────────────────────

    def _provision_queues(self, resources: list[CFNResource]) -> None:
        """Two-pass queue provisioning.

        Pass 1 creates every queue without its RedrivePolicy — at this
        point we don't yet have the DLQs' ARNs in ``self._refs`` because
        the source queue may appear before the DLQ in CFN order.

        Pass 2 walks the same list and applies RedrivePolicy via
        ``set_queue_attributes`` on the queues that declared one,
        resolving the ``Fn::GetAtt`` reference to the DLQ via the now-
        populated ``self._refs`` map.
        """
        for r in resources:
            name = r.properties.get("QueueName") or r.logical_id
            attrs: dict[str, str] = {}
            if r.properties.get("FifoQueue"):
                attrs["FifoQueue"] = "true"
            if r.properties.get("ContentBasedDeduplication"):
                attrs["ContentBasedDeduplication"] = "true"
            if "VisibilityTimeout" in r.properties:
                attrs["VisibilityTimeout"] = str(r.properties["VisibilityTimeout"])
            if "MessageRetentionPeriod" in r.properties:
                attrs["MessageRetentionPeriod"] = str(
                    r.properties["MessageRetentionPeriod"]
                )
            resp = self.sqs.create_queue(QueueName=name, Attributes=attrs)
            url = resp["QueueUrl"]
            self._refs[r.logical_id] = url
            console.print(f"  ✓ SQS queue [cyan]{name}[/cyan] → {url}")

        # Pass 2: redrive policies. RedrivePolicy.deadLetterTargetArn is
        # a CFN Fn::GetAtt against the DLQ's logical ID; resolve it via
        # the refs map we just populated.
        for r in resources:
            policy = r.properties.get("RedrivePolicy")
            if not policy:
                continue
            url = self._refs.get(r.logical_id)
            if url is None:
                console.print(
                    f"  [yellow]! skipping RedrivePolicy on {r.logical_id}: "
                    f"queue not provisioned[/yellow]"
                )
                continue
            target_arn = self._resolve(policy.get("deadLetterTargetArn"))
            max_count = policy.get("maxReceiveCount")
            if target_arn is None or max_count is None:
                console.print(
                    f"  [yellow]! skipping RedrivePolicy on {r.logical_id}: "
                    f"unresolved target or missing maxReceiveCount[/yellow]"
                )
                continue
            redrive = {
                "deadLetterTargetArn": target_arn,
                "maxReceiveCount": int(max_count),
            }
            self.sqs.set_queue_attributes(
                QueueUrl=url,
                Attributes={"RedrivePolicy": json.dumps(redrive)},
            )
            console.print(
                f"  ✓ SQS redrive [cyan]{r.logical_id}[/cyan] → "
                f"{target_arn} (maxReceiveCount={max_count})"
            )

    # ── S3 ────────────────────────────────────────────────────────────────────

    def _provision_buckets(self, resources: list[CFNResource]) -> None:
        for r in resources:
            name = r.properties.get("BucketName") or r.logical_id.lower()
            try:
                self.s3.create_bucket(Bucket=name)
                console.print(f"  ✓ S3 bucket [cyan]{name}[/cyan]")
            except self.s3.exceptions.BucketAlreadyOwnedByYou:
                console.print(f"  ✓ S3 bucket [cyan]{name}[/cyan] (already exists)")
            self._refs[r.logical_id] = name

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def _provision_subscriptions(self, resources: list[CFNResource]) -> None:
        for r in resources:
            topic_arn = self._resolve(r.properties.get("TopicArn"))
            endpoint = self._resolve(r.properties.get("Endpoint"))
            protocol = r.properties.get("Protocol", "sqs")
            if topic_arn is None or endpoint is None:
                console.print(
                    f"  [yellow]! skipping subscription {r.logical_id}: "
                    f"unresolved TopicArn or Endpoint[/yellow]"
                )
                continue
            attrs: dict[str, str] = {}
            if r.properties.get("RawMessageDelivery"):
                attrs["RawMessageDelivery"] = "true"
            kwargs: dict[str, Any] = dict(
                TopicArn=topic_arn,
                Protocol=protocol,
                Endpoint=self._sqs_arn_for_subscription(endpoint, protocol),
            )
            if attrs:
                kwargs["Attributes"] = attrs
            resp = self.sns.subscribe(**kwargs)
            console.print(
                f"  ✓ SNS subscription {protocol} → "
                f"[cyan]{endpoint}[/cyan] ({resp['SubscriptionArn']})"
            )

    def _sqs_arn_for_subscription(self, endpoint: str, protocol: str) -> str:
        """SNS subscriptions take an SQS ARN as endpoint, not a URL.

        CFN may resolve to either form depending on whether the CFN
        template uses ``Ref`` (URL) or ``Fn::GetAtt: Arn`` (ARN). Detect
        the ARN form by its prefix and return it unchanged; for URLs,
        derive the ARN. Moto's format mirrors real AWS:
        ``arn:aws:sqs:<region>:<account>:<name>``.
        """
        if protocol != "sqs":
            return endpoint
        if endpoint.startswith("arn:aws:sqs:"):
            # Already an ARN — don't double-prefix.
            return endpoint
        # endpoint is an SQS URL — derive the ARN.
        name = endpoint.rstrip("/").rsplit("/", 1)[-1]
        return f"arn:aws:sqs:{_FAKE_REGION}:{_FAKE_ACCOUNT}:{name}"

    # ── CFN reference resolution (delegates to synth.resolve_value) ───────────

    def _resolve(self, value: Any) -> str | None:
        from treadmill_local.synth import resolve_value
        return resolve_value(value, self._refs)

    def get_refs(self) -> dict[str, str]:
        """Return a copy of the logical-ID → ARN/URL map after provisioning."""
        return dict(self._refs)
