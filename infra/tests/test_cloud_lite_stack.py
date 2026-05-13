"""Assertions on the synthesized ``TreadmillCloudLite`` template.

The CloudLite stack composes ``MessagingConstruct`` (SNS + SQS) and the
placeholder ``SecretsConstruct``. Tests check three things:

1. Each messaging resource exists with its deployment-suffixed physical name.
2. Every taggable resource carries ``treadmill:deployment_id=<deployment_id>``
   (ADR-0016 cost-attribution + multi-deployment-confusion guard).
3. The ``deployment_id`` constructor argument rejects malformed values
   (ADR-0016 regex ``^[a-z][a-z0-9]{0,29}$``) and accepts well-formed ones.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from treadmill_infra.stacks import TreadmillCloudLite
from treadmill_infra.stacks.cloud_lite import _stack_name_for


def _template(deployment_id: str = "test") -> assertions.Template:
    app = cdk.App()
    stack = TreadmillCloudLite(
        app, _stack_name_for(deployment_id), deployment_id=deployment_id,
    )
    return assertions.Template.from_stack(stack)


# ── Stack-name derivation ─────────────────────────────────────────────────────


def test_stack_name_pascal_case():
    """``personal`` → ``TreadmillPersonalCloudLite`` per ADR-0016."""
    assert _stack_name_for("personal") == "TreadmillPersonalCloudLite"
    assert _stack_name_for("strongdm") == "TreadmillStrongdmCloudLite"
    assert _stack_name_for("test") == "TreadmillTestCloudLite"


# ── Messaging resources (deployment-suffixed names) ───────────────────────────


def test_work_queue_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": "treadmill-test-work.fifo",
            "FifoQueue": True,
            "ContentBasedDeduplication": True,
        },
    )


def test_work_dlq_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": "treadmill-test-work-dlq.fifo",
            "FifoQueue": True,
            "ContentBasedDeduplication": True,
        },
    )


def test_events_topic_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::SNS::Topic",
        {"TopicName": "treadmill-test-events"},
    )


def test_coordination_queue_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": "treadmill-test-coordination"},
    )


def test_coordination_dlq_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": "treadmill-test-coordination-dlq"},
    )


def test_events_topic_subscribes_coordination_queue_raw():
    """Subscription must use raw message delivery — the API consumer parses
    JSON event bodies directly instead of unwrapping SNS envelopes."""
    template = _template()
    subs = template.find_resources(
        "AWS::SNS::Subscription",
        {"Properties": {"RawMessageDelivery": True, "Protocol": "sqs"}},
    )
    assert len(subs) == 1, (
        f"expected exactly 1 raw-delivery sqs sub, got {len(subs)}"
    )


def test_work_queue_redrive_policy():
    """Work queue redrives to its FIFO DLQ after 3 failed receives
    (decision #11, 2026-05-11 closure plan)."""
    template = _template()
    queues = template.find_resources(
        "AWS::SQS::Queue",
        {"Properties": {"QueueName": "treadmill-test-work.fifo"}},
    )
    assert len(queues) == 1
    [(_lid, queue)] = queues.items()
    redrive = queue["Properties"].get("RedrivePolicy")
    assert redrive is not None, "work queue must have a RedrivePolicy"
    assert redrive["maxReceiveCount"] == 3


def test_coordination_queue_redrive_policy():
    """Coordination queue redrives to its DLQ after 5 failed receives —
    ~5 min of retries against a 60s visibility timeout."""
    template = _template()
    queues = template.find_resources(
        "AWS::SQS::Queue",
        {"Properties": {"QueueName": "treadmill-test-coordination"}},
    )
    assert len(queues) == 1
    [(_lid, queue)] = queues.items()
    redrive = queue["Properties"].get("RedrivePolicy")
    assert redrive is not None, "coordination queue must have a RedrivePolicy"
    assert redrive["maxReceiveCount"] == 5


def test_deploy_events_queue_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": "treadmill-test-deploy-events"},
    )


def test_deploy_events_dlq_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": "treadmill-test-deploy-events-dlq"},
    )


def test_deploy_events_queue_redrive_policy():
    """Deploy-events queue redrives to its DLQ after 3 failed receives."""
    template = _template()
    queues = template.find_resources(
        "AWS::SQS::Queue",
        {"Properties": {"QueueName": "treadmill-test-deploy-events"}},
    )
    assert len(queues) == 1
    [(_lid, queue)] = queues.items()
    redrive = queue["Properties"].get("RedrivePolicy")
    assert redrive is not None, "deploy-events queue must have a RedrivePolicy"
    assert redrive["maxReceiveCount"] == 3


def test_deploy_events_queue_visibility_timeout():
    """Deploy-events queue has 30s visibility timeout."""
    template = _template()
    queues = template.find_resources(
        "AWS::SQS::Queue",
        {"Properties": {"QueueName": "treadmill-test-deploy-events"}},
    )
    assert len(queues) == 1
    [(_lid, queue)] = queues.items()
    assert queue["Properties"]["VisibilityTimeout"] == 30


def test_deploy_events_sns_subscription_with_filter_policy():
    """Deploy-events queue is subscribed to events topic with filter policy
    limiting to github PRs merged."""
    template = _template().to_json()
    resources = template.get("Resources", {})

    # Find SNS subscriptions with FilterPolicy
    found_deploy_events_sub = False
    for resource in resources.values():
        if resource.get("Type") != "AWS::SNS::Subscription":
            continue
        props = resource.get("Properties", {})
        filter_policy = props.get("FilterPolicy")
        if filter_policy is None:
            continue
        # Check if filter policy has entity_type=github AND action=pr_merged
        if (
            isinstance(filter_policy, dict)
            and "entity_type" in filter_policy
            and "action" in filter_policy
            and filter_policy.get("entity_type") == ["github"]
            and filter_policy.get("action") == ["pr_merged"]
        ):
            found_deploy_events_sub = True
            break

    assert found_deploy_events_sub, (
        "expected SNS subscription with filter policy "
        "(entity_type=['github'] AND action=['pr_merged'])"
    )


def test_dlqs_have_14_day_retention():
    template = _template()
    for name in (
        "treadmill-test-work-dlq.fifo",
        "treadmill-test-coordination-dlq",
        "treadmill-test-deploy-events-dlq",
    ):
        dlqs = template.find_resources(
            "AWS::SQS::Queue", {"Properties": {"QueueName": name}},
        )
        assert len(dlqs) == 1, f"expected DLQ {name}"
        [(_lid, dlq)] = dlqs.items()
        assert dlq["Properties"]["MessageRetentionPeriod"] == 14 * 24 * 60 * 60


def test_sns_topic_count_after_phase_b_composition():
    """Two topics post-Phase-B composition:

    - ``treadmill-<id>-events`` (messaging)
    - ``treadmill-<id>-billing-alarms`` (observability)

    No work-fanout topic sits in front of the FIFO work queue.
    """
    template = _template()
    template.resource_count_is("AWS::SNS::Topic", 2)


def test_deploy_events_cfn_outputs():
    """Two CloudFormation outputs for deploy-events:
    - DeployEventsQueueUrl
    - DeployEventsDlqUrl
    """
    template = _template().to_json()
    outputs = template.get("Outputs", {})
    output_keys = set(outputs.keys())

    # CDK appends an 8-char hash to the logical ID
    queue_output_keys = [key for key in output_keys if "DeployEventsQueueUrl" in key]
    dlq_output_keys = [key for key in output_keys if "DeployEventsDlqUrl" in key]

    assert len(queue_output_keys) == 1, (
        f"expected exactly 1 DeployEventsQueueUrl output, got {queue_output_keys}"
    )
    assert len(dlq_output_keys) == 1, (
        f"expected exactly 1 DeployEventsDlqUrl output, got {dlq_output_keys}"
    )


def test_resource_count_is_minimal():
    """CloudLite holds only the cloud-lite shape — no VPC, no ECS, no S3.
    Compute (API, Postgres, Redis, workers) stays on the laptop.

    Expected counts after Phase B + ADR-0023 + deploy-events composition:
      - SQS queues: 8 (work, work-dlq, coordination, coord-dlq,
        webhook-inbox, webhook-inbox-dlq, deploy-events, deploy-events-dlq).
      - SNS topics: 2 (events, billing-alarms).
      - Secrets Manager: 4 (github-webhook-secret, github-pat,
        worker-aws-credentials, api-aws-credentials per ADR-0023).
      - Lambda: 1 (webhook receiver).
      - CloudWatch Alarm: 1 (monthly billing).
      - Zero VPC / ECS / S3 — compute is local per ADR-0016.
    """
    template = _template()
    template.resource_count_is("AWS::SQS::Queue", 8)
    template.resource_count_is("AWS::SNS::Topic", 2)
    template.resource_count_is("AWS::SecretsManager::Secret", 4)
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("AWS::CloudWatch::Alarm", 1)
    template.resource_count_is("AWS::EC2::VPC", 0)
    template.resource_count_is("AWS::ECS::Cluster", 0)
    template.resource_count_is("AWS::S3::Bucket", 0)


# ── Tagging (every taggable resource inherits deployment_id tag) ──────────────


def _tag_pairs(props: dict) -> list[tuple[str, str]]:
    """Extract ``(Key, Value)`` pairs from a resource's ``Tags`` property."""
    return [(t["Key"], t["Value"]) for t in props.get("Tags", [])]


def test_every_taggable_resource_carries_deployment_id_tag():
    """ADR-0016 §"Cost attribution backstop": every taggable resource in
    the synthesized template must carry ``treadmill:deployment_id=test``.
    CDK's ``Tags.of(self).add(...)`` at the stack level cascades through
    every CFN resource that supports a ``Tags`` property."""
    template = _template().to_json()
    resources = template["Resources"]

    expected = ("treadmill:deployment_id", "test")
    taggable_types = {
        "AWS::SQS::Queue",
        "AWS::SNS::Topic",
    }

    seen_any = False
    for logical_id, resource in resources.items():
        if resource["Type"] not in taggable_types:
            continue
        seen_any = True
        tags = _tag_pairs(resource.get("Properties", {}))
        assert expected in tags, (
            f"resource {logical_id} ({resource['Type']}) is missing tag "
            f"{expected[0]}={expected[1]}; got {tags}"
        )
    assert seen_any, "expected at least one taggable resource in the template"


def test_deployment_id_tag_uses_canonical_key_spelling():
    """The tag key is ``treadmill:deployment_id`` — colon-separated
    namespace + lower_snake. Drift in this spelling breaks the operator's
    ``aws resourcegroupstaggingapi`` query, so the spelling itself is
    load-bearing and asserted here."""
    template = _template().to_json()
    resources = template["Resources"]
    for resource in resources.values():
        for tag in resource.get("Properties", {}).get("Tags", []):
            if tag["Key"] != "treadmill:deployment_id":
                continue
            # Found the key in its canonical spelling — value matches the
            # deployment_id passed at construction.
            assert tag["Value"] == "test"
            return
    pytest.fail("treadmill:deployment_id tag not found on any resource")


# ── deployment_id validation ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "deployment_id",
    [
        "personal",
        "strongdm",
        "a",                                  # 1 char, lowercase letter
        "abc123",                             # alphanumeric
        "a" * 30,                             # 30 chars (max)
        "test1",
    ],
)
def test_valid_deployment_ids_accepted(deployment_id: str):
    app = cdk.App()
    stack = TreadmillCloudLite(
        app,
        _stack_name_for(deployment_id),
        deployment_id=deployment_id,
    )
    assert stack.deployment_id == deployment_id


@pytest.mark.parametrize(
    "deployment_id",
    [
        "",                                    # empty
        "1personal",                           # starts with digit
        "Personal",                            # uppercase
        "perSonal",                            # uppercase mid-string
        "personal_dev",                        # underscore
        "personal-dev",                        # hyphen
        "personal.dev",                        # dot
        "personal dev",                        # space
        "a" * 31,                              # over 30 chars
    ],
)
def test_invalid_deployment_ids_rejected(deployment_id: str):
    app = cdk.App()
    with pytest.raises(ValueError, match="invalid deployment_id"):
        TreadmillCloudLite(
            app,
            "TestStack",
            deployment_id=deployment_id,
        )


def test_non_string_deployment_id_rejected():
    """Defensive: a non-string ``deployment_id`` (e.g. ``None`` from a
    missing CDK context value) should fail loud, not at synth time with
    an opaque CFN-template error."""
    app = cdk.App()
    with pytest.raises(ValueError, match="invalid deployment_id"):
        TreadmillCloudLite(app, "TestStack", deployment_id=None)  # type: ignore[arg-type]
