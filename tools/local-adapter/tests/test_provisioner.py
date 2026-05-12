"""Integration tests for MotoProvisioner against in-process moto."""

from __future__ import annotations

import json
import time

import boto3
import pytest

from treadmill_local.provisioner import MotoProvisioner
from treadmill_local.synth import CFNResource, SynthResult


def _result(*resources: CFNResource) -> SynthResult:
    return SynthResult(
        stack_name="Test",
        template_path=None,  # type: ignore[arg-type]
        template={},
        resources=list(resources),
    )


def _client(service: str, endpoint: str):
    return boto3.client(
        service,
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_provision_sns_topic(moto_server: str):
    provisioner = MotoProvisioner(moto_server)
    provisioner.provision(
        _result(
            CFNResource(
                "SomeFifoTopic",
                "AWS::SNS::Topic",
                {"TopicName": "work.fifo", "FifoTopic": True},
            ),
        )
    )
    sns = _client("sns", moto_server)
    arns = [t["TopicArn"] for t in sns.list_topics()["Topics"]]
    assert any(arn.endswith(":work.fifo") for arn in arns)


def test_provision_sqs_fifo_queue(moto_server: str):
    provisioner = MotoProvisioner(moto_server)
    provisioner.provision(
        _result(
            CFNResource(
                "WorkQueue",
                "AWS::SQS::Queue",
                {
                    "QueueName": "work.fifo",
                    "FifoQueue": True,
                    "ContentBasedDeduplication": True,
                    "VisibilityTimeout": 60,
                },
            ),
        )
    )
    sqs = _client("sqs", moto_server)
    urls = sqs.list_queues().get("QueueUrls", [])
    assert any(u.endswith("/work.fifo") for u in urls)


def test_provision_s3_bucket(moto_server: str):
    provisioner = MotoProvisioner(moto_server)
    provisioner.provision(
        _result(
            CFNResource("Artifacts", "AWS::S3::Bucket", {"BucketName": "my-bucket"}),
        )
    )
    s3 = _client("s3", moto_server)
    names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert "my-bucket" in names


def test_sns_to_sqs_subscription_round_trip(moto_server: str):
    """The full path: topic + queue + subscription, then publish/receive."""
    provisioner = MotoProvisioner(moto_server)
    provisioner.provision(
        _result(
            CFNResource(
                "SomeFifoTopic",
                "AWS::SNS::Topic",
                {"TopicName": "work.fifo", "FifoTopic": True, "ContentBasedDeduplication": True},
            ),
            CFNResource(
                "WorkQueue",
                "AWS::SQS::Queue",
                {"QueueName": "work.fifo", "FifoQueue": True, "ContentBasedDeduplication": True},
            ),
            CFNResource(
                "Sub",
                "AWS::SNS::Subscription",
                {
                    "TopicArn": {"Ref": "SomeFifoTopic"},
                    "Endpoint": {"Fn::GetAtt": ["WorkQueue", "Arn"]},
                    "Protocol": "sqs",
                    "RawMessageDelivery": True,
                },
            ),
        )
    )

    sns = _client("sns", moto_server)
    sqs = _client("sqs", moto_server)
    topic_arn = next(t["TopicArn"] for t in sns.list_topics()["Topics"])
    queue_url = next(iter(sqs.list_queues()["QueueUrls"]))

    sns.publish(
        TopicArn=topic_arn,
        Message=json.dumps({"hello": "treadmill"}),
        MessageGroupId="g1",
        MessageDeduplicationId="m1",
    )

    # Give moto's subscription pump a beat.
    deadline = time.monotonic() + 5.0
    msgs: list[dict] = []
    while time.monotonic() < deadline and not msgs:
        resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
        msgs = resp.get("Messages", [])
    assert msgs, "no message received via SNS→SQS subscription"
    assert msgs[0]["Body"] == json.dumps({"hello": "treadmill"})


def test_subscription_with_unresolved_ref_is_skipped(moto_server: str):
    """A subscription pointing at a logical ID we never provisioned must be
    skipped rather than crash."""
    provisioner = MotoProvisioner(moto_server)
    # No topic/queue created — Ref/GetAtt will not resolve.
    provisioner.provision(
        _result(
            CFNResource(
                "Sub",
                "AWS::SNS::Subscription",
                {
                    "TopicArn": {"Ref": "Missing"},
                    "Endpoint": {"Fn::GetAtt": ["AlsoMissing", "Arn"]},
                    "Protocol": "sqs",
                },
            ),
        )
    )
    # If we got here without raising, the skip path works.


def test_provision_queue_with_redrive_policy_and_retention(moto_server: str):
    """A source queue declaring RedrivePolicy + a DLQ resource (with its
    own MessageRetentionPeriod) end up wired together: the source carries
    a RedrivePolicy attribute pointing at the DLQ's ARN; the DLQ carries
    the requested retention. Per the 2026-05-11 closure plan C.2 — the
    spike adapter must operationalize CFN-declared DLQs locally."""
    provisioner = MotoProvisioner(moto_server)
    provisioner.provision(
        _result(
            CFNResource(
                "EventsDlq",
                "AWS::SQS::Queue",
                {
                    "QueueName": "events-dlq",
                    "MessageRetentionPeriod": 1209600,
                },
            ),
            CFNResource(
                "EventsQueue",
                "AWS::SQS::Queue",
                {
                    "QueueName": "events",
                    "RedrivePolicy": {
                        "deadLetterTargetArn": {"Fn::GetAtt": ["EventsDlq", "Arn"]},
                        "maxReceiveCount": 5,
                    },
                },
            ),
        )
    )

    sqs = _client("sqs", moto_server)
    # Locate the queues by name.
    urls = sqs.list_queues().get("QueueUrls", [])
    main_url = next(u for u in urls if u.endswith("/events"))
    dlq_url = next(u for u in urls if u.endswith("/events-dlq"))

    main_attrs = sqs.get_queue_attributes(
        QueueUrl=main_url, AttributeNames=["RedrivePolicy"],
    )["Attributes"]
    policy = json.loads(main_attrs["RedrivePolicy"])
    assert policy["maxReceiveCount"] == 5
    assert policy["deadLetterTargetArn"].endswith(":events-dlq")

    dlq_attrs = sqs.get_queue_attributes(
        QueueUrl=dlq_url, AttributeNames=["MessageRetentionPeriod"],
    )["Attributes"]
    assert int(dlq_attrs["MessageRetentionPeriod"]) == 1209600


def test_provision_fifo_queue_with_fifo_dlq(moto_server: str):
    """A FIFO source queue must redrive to a FIFO DLQ — the spike's work
    queue. Verify the two-pass provisioning handles FIFO-on-both-sides
    correctly (CFN ordering may put the source queue before its DLQ;
    the second pass must still resolve the GetAtt because pass 1
    created both)."""
    provisioner = MotoProvisioner(moto_server)
    provisioner.provision(
        _result(
            CFNResource(
                "WorkQueue",
                "AWS::SQS::Queue",
                {
                    "QueueName": "work.fifo",
                    "FifoQueue": True,
                    "ContentBasedDeduplication": True,
                    "RedrivePolicy": {
                        "deadLetterTargetArn": {"Fn::GetAtt": ["WorkDlq", "Arn"]},
                        "maxReceiveCount": 3,
                    },
                },
            ),
            CFNResource(
                "WorkDlq",
                "AWS::SQS::Queue",
                {
                    "QueueName": "work-dlq.fifo",
                    "FifoQueue": True,
                    "ContentBasedDeduplication": True,
                    "MessageRetentionPeriod": 1209600,
                },
            ),
        )
    )

    sqs = _client("sqs", moto_server)
    urls = sqs.list_queues().get("QueueUrls", [])
    main_url = next(u for u in urls if u.endswith("/work.fifo"))

    main_attrs = sqs.get_queue_attributes(
        QueueUrl=main_url,
        AttributeNames=["RedrivePolicy", "FifoQueue"],
    )["Attributes"]
    policy = json.loads(main_attrs["RedrivePolicy"])
    assert policy["maxReceiveCount"] == 3
    assert policy["deadLetterTargetArn"].endswith(":work-dlq.fifo")
    assert main_attrs.get("FifoQueue") == "true"


def test_sqs_arn_for_subscription_handles_arn_input() -> None:
    """``_sqs_arn_for_subscription`` must NOT double-prefix when given an
    ARN (which is what ``Fn::GetAtt: Arn`` resolves to). The pre-fix code
    blindly composed ``arn:aws:sqs:<region>:<account>:{name}`` where
    ``name = endpoint.rsplit('/', 1)[-1]`` — for an ARN input (no
    slashes) the entire ARN became ``name`` and the result was
    ``arn:aws:sqs:...arn:aws:sqs:...``. That broke SNS->SQS delivery in
    the live substrate, blocking every end-to-end integration test.

    Both input forms must produce the same canonical ARN."""
    prov = MotoProvisioner("http://localhost:5000")
    url = "http://localhost:5000/123456789012/work.fifo"
    arn = "arn:aws:sqs:us-east-1:123456789012:work.fifo"
    expected = "arn:aws:sqs:us-east-1:123456789012:work.fifo"
    assert prov._sqs_arn_for_subscription(url, "sqs") == expected
    assert prov._sqs_arn_for_subscription(arn, "sqs") == expected
    # Non-SQS protocols pass through unchanged.
    assert prov._sqs_arn_for_subscription("https://example.com/x", "https") == "https://example.com/x"
