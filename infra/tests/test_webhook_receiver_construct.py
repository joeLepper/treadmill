"""Assertions on the synthesized ``WebhookReceiverConstruct`` template.

The construct provisions four resources (per ADR-0017 §"CDK resources"):

1. SQS standard queue ``treadmill-<deployment_id>-webhook-inbox`` (60s visibility,
   14d retention, DLQ-wired with ``maxReceiveCount=5``).
2. SQS DLQ ``treadmill-<deployment_id>-webhook-inbox-dlq`` (14d retention).
3. Lambda function ``treadmill-<deployment_id>-webhook-receiver`` (Python 3.12,
   ``WEBHOOK_INBOX_QUEUE_URL`` env var, exactly-one ``sqs:SendMessage`` grant).
4. API Gateway HTTP API ``treadmill-<deployment_id>-webhook-api`` with
   ``POST /webhook/github`` integrated to the Lambda.

Also asserted: the three CFN outputs (``WebhookApiUrl``,
``WebhookInboxQueueUrl``, ``WebhookInboxDlqUrl``) and the
``treadmill:deployment_id`` tag on every taggable resource.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions

from treadmill_infra.constructs.webhook_receiver import WebhookReceiverConstruct


# ── Test stack fixture ────────────────────────────────────────────────────────


def _template(deployment_id: str = "test") -> assertions.Template:
    """Synthesize ``WebhookReceiverConstruct`` inside a throwaway test stack.

    The test stack applies the ``treadmill:deployment_id`` tag at the stack
    level — same pattern as ``TreadmillCloudLite`` — so the tagging
    assertions below see what a real composition would see.
    """
    app = cdk.App()
    stack = cdk.Stack(app, "TestStack")
    cdk.Tags.of(stack).add("treadmill:deployment_id", deployment_id)
    WebhookReceiverConstruct(
        stack, "WebhookReceiver", deployment_id=deployment_id,
    )
    return assertions.Template.from_stack(stack)


# ── Resource existence + naming ───────────────────────────────────────────────


def test_webhook_inbox_queue_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": "treadmill-test-webhook-inbox"},
    )


def test_webhook_inbox_dlq_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": "treadmill-test-webhook-inbox-dlq"},
    )


def test_webhook_lambda_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "treadmill-test-webhook-receiver",
            "Runtime": "python3.12",
            "Handler": "handler.handler",
        },
    )


def test_webhook_http_api_with_deployment_suffix():
    template = _template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {
            "Name": "treadmill-test-webhook-api",
            "ProtocolType": "HTTP",
        },
    )


# ── Lambda environment variable ───────────────────────────────────────────────


def test_lambda_env_has_webhook_inbox_queue_url():
    """Lambda must receive ``WEBHOOK_INBOX_QUEUE_URL`` — ADR-0017's canonical
    spelling. The value is a CFN Ref to the inbox queue, not a literal."""
    template = _template()
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"FunctionName": "treadmill-test-webhook-receiver"}},
    )
    assert len(functions) == 1
    [(_lid, fn)] = functions.items()
    env_vars = fn["Properties"].get("Environment", {}).get("Variables", {})
    assert "WEBHOOK_INBOX_QUEUE_URL" in env_vars, (
        f"Lambda env missing WEBHOOK_INBOX_QUEUE_URL; got {list(env_vars)}"
    )
    # The value should reference the queue (a CFN Ref / GetAtt), not a
    # hardcoded URL. We don't pin the exact shape (CDK token rendering
    # may vary), but it must not be an empty string.
    assert env_vars["WEBHOOK_INBOX_QUEUE_URL"], (
        "WEBHOOK_INBOX_QUEUE_URL value is empty"
    )


# ── Lambda IAM scope (load-bearing — ADR-0017 ALLOWS ONLY sqs:SendMessage) ───


def test_lambda_iam_policy_grants_only_send_message_on_inbox_queue():
    """Per ADR-0017: the Lambda's IAM grant is ``queue.grant_send_messages(fn)``
    and nothing else. CDK's ``grant_send_messages`` ships ``sqs:SendMessage``
    plus the two read-metadata helpers ``sqs:GetQueueUrl`` +
    ``sqs:GetQueueAttributes`` that boto3's SQS client may invoke
    incidentally; all three are scoped to the inbox queue's ARN. The
    regression net here catches: (a) any non-SQS action sneaking in,
    (b) any non-inbox-queue resource (notably the DLQ — the Lambda must
    not write to the DLQ), (c) more than one Statement.

    CloudWatch Logs comes via the managed ``AWSLambdaBasicExecutionRole``
    policy CDK attaches to the function's role automatically — that's an
    ``AWS::IAM::Role`` ManagedPolicyArn, NOT an inline ``AWS::IAM::Policy``,
    so it doesn't show up here.
    """
    template = _template()
    policies = template.find_resources("AWS::IAM::Policy")
    # Exactly one inline policy on the Lambda's role (the queue grant).
    assert len(policies) == 1, (
        f"expected exactly 1 inline IAM Policy (the SQS grant); "
        f"got {len(policies)}: {list(policies)}"
    )
    [(_lid, policy)] = policies.items()
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    assert len(statements) == 1, (
        f"expected exactly 1 Statement in the policy; got {len(statements)}"
    )
    stmt = statements[0]
    assert stmt["Effect"] == "Allow"
    # Action: single string or list — coerce to set for inspection.
    actions = stmt["Action"]
    if isinstance(actions, str):
        actions = [actions]
    actions_set = set(actions)
    # ``sqs:SendMessage`` is mandatory; the only other actions that may
    # appear are the read-metadata helpers CDK's ``grant_send_messages``
    # bundles. Anything else (e.g. ``sqs:ReceiveMessage``,
    # ``sqs:DeleteMessage``, any non-SQS action) is a regression.
    assert "sqs:SendMessage" in actions_set, (
        f"missing sqs:SendMessage; got {actions_set}"
    )
    allowed = {"sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"}
    assert actions_set <= allowed, (
        f"Lambda IAM policy has unexpected actions beyond "
        f"queue.grant_send_messages's canonical set; got {actions_set}, "
        f"allowed {allowed}"
    )
    # Resource is a CFN GetAtt against the inbox queue's Arn. We don't pin
    # the exact logical id but every GetAtt target must reference the
    # inbox queue (not the DLQ, not some other queue).
    resource = stmt["Resource"]
    getatts: list[list[str]] = []

    def _collect(node):
        if isinstance(node, dict):
            if "Fn::GetAtt" in node:
                getatts.append(node["Fn::GetAtt"])
            for v in node.values():
                _collect(v)
        elif isinstance(node, list):
            for v in node:
                _collect(v)

    _collect(resource)
    assert getatts, f"expected Fn::GetAtt in Resource; got {resource}"
    # Every GetAtt target's logical id must match the inbox queue's id
    # (CDK prefixes with the construct path: "WebhookReceiverWebhookInboxQueue...").
    for target, attr in getatts:
        assert "WebhookInboxQueue" in target and "Dlq" not in target, (
            f"expected GetAtt against the inbox queue (not the DLQ); "
            f"got {target}"
        )
        assert attr == "Arn"


# ── DLQ wiring + visibility timeout ───────────────────────────────────────────


def test_webhook_inbox_redrive_policy_max_receive_count_5():
    """The inbox queue must redrive to the DLQ after 5 failed receives
    (ADR-0017's chosen value — buys headroom against transient DB or
    SNS-publish failures before declaring poison)."""
    template = _template()
    queues = template.find_resources(
        "AWS::SQS::Queue",
        {"Properties": {"QueueName": "treadmill-test-webhook-inbox"}},
    )
    assert len(queues) == 1
    [(_lid, queue)] = queues.items()
    redrive = queue["Properties"].get("RedrivePolicy")
    assert redrive is not None, "inbox queue must have a RedrivePolicy"
    assert redrive["maxReceiveCount"] == 5


def test_webhook_inbox_visibility_timeout_60s():
    """ADR-0017 §"CDK resources" pins the visibility timeout at 60s."""
    template = _template()
    queues = template.find_resources(
        "AWS::SQS::Queue",
        {"Properties": {"QueueName": "treadmill-test-webhook-inbox"}},
    )
    assert len(queues) == 1
    [(_lid, queue)] = queues.items()
    assert queue["Properties"]["VisibilityTimeout"] == 60


def test_webhook_inbox_retention_14_days():
    template = _template()
    queues = template.find_resources(
        "AWS::SQS::Queue",
        {"Properties": {"QueueName": "treadmill-test-webhook-inbox"}},
    )
    [(_lid, queue)] = queues.items()
    assert queue["Properties"]["MessageRetentionPeriod"] == 14 * 24 * 60 * 60


def test_webhook_inbox_dlq_retention_14_days():
    template = _template()
    queues = template.find_resources(
        "AWS::SQS::Queue",
        {"Properties": {"QueueName": "treadmill-test-webhook-inbox-dlq"}},
    )
    [(_lid, queue)] = queues.items()
    assert queue["Properties"]["MessageRetentionPeriod"] == 14 * 24 * 60 * 60


# ── API Gateway route ─────────────────────────────────────────────────────────


def test_http_api_has_post_webhook_github_route():
    """Route key ``POST /webhook/github`` exists at v0. Adding
    ``POST /webhook/slack`` later is a one-line addition to the route
    table in ``webhook_receiver.py``."""
    template = _template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Route",
        {"RouteKey": "POST /webhook/github"},
    )


def test_http_api_route_integrates_with_webhook_lambda():
    """The ``POST /webhook/github`` route must integrate with the webhook
    Lambda (not some other function). The route's ``Target`` references an
    integration; the integration's ``IntegrationUri`` is a ``GetAtt`` /
    ``Fn::Join`` chain that resolves to the Lambda's invoke ARN."""
    template = _template()
    integrations = template.find_resources(
        "AWS::ApiGatewayV2::Integration",
        {"Properties": {"IntegrationType": "AWS_PROXY"}},
    )
    assert len(integrations) == 1, (
        f"expected exactly 1 AWS_PROXY integration; got {len(integrations)}"
    )
    [(_lid, integration)] = integrations.items()
    uri = integration["Properties"]["IntegrationUri"]

    # The IntegrationUri is a GetAtt against the Lambda function. Walk the
    # nested structure and assert the logical id references the webhook
    # Lambda (CDK prefixes with the construct path).
    getatts: list[list[str]] = []

    def _collect(node):
        if isinstance(node, dict):
            if "Fn::GetAtt" in node:
                getatts.append(node["Fn::GetAtt"])
            for v in node.values():
                _collect(v)
        elif isinstance(node, list):
            for v in node:
                _collect(v)

    _collect(uri)
    assert getatts, f"expected Fn::GetAtt in IntegrationUri; got {uri}"
    assert any("WebhookLambda" in target for target, _ in getatts), (
        f"integration target should reference the webhook Lambda; "
        f"got {getatts}"
    )


# ── CFN outputs ───────────────────────────────────────────────────────────────


def test_cfn_outputs_present():
    """Three CFN outputs feed ``treadmill-local init`` per ADR-0016: the
    API URL, the inbox queue URL, the inbox DLQ URL."""
    template = _template().to_json()
    outputs = template.get("Outputs", {})
    # CDK prefixes the construct path onto the logical id of CfnOutput;
    # find each by matching the suffix.
    # CDK appends an 8-char hash suffix to CfnOutput logical ids and
    # prefixes them with the construct path. Match by substring; the
    # construct's CfnOutput names are unique enough that substring is
    # unambiguous (e.g. "WebhookApiUrl" doesn't appear inside any other
    # output's name).
    output_ids = list(outputs.keys())
    expected_substrings = (
        "WebhookApiUrl",
        "WebhookInboxQueueUrl",
        "WebhookInboxDlqUrl",
    )
    for needle in expected_substrings:
        assert any(needle in oid for oid in output_ids), (
            f"expected an output whose logical id contains {needle!r}; "
            f"got {output_ids}"
        )


# ── Tagging (every taggable resource inherits deployment_id tag) ──────────────


def _resource_has_tag(props: dict, key: str, value: str) -> bool:
    """Return True iff the resource's ``Tags`` property contains ``key=value``.

    CloudFormation resources render ``Tags`` in two shapes depending on
    the underlying service: most resources (SQS, Lambda, SNS, etc.) use
    the list-of-``{Key, Value}``-pairs convention; API Gateway v2
    resources render tags as a flat ``{key: value}`` dict. This helper
    normalizes both.
    """
    tags = props.get("Tags")
    if tags is None:
        return False
    if isinstance(tags, dict):
        return tags.get(key) == value
    # List-of-pairs shape.
    return any(
        isinstance(t, dict) and t.get("Key") == key and t.get("Value") == value
        for t in tags
    )


def test_taggable_resources_carry_deployment_id_tag():
    """Each taggable resource type in the construct (Queue, DLQ, Lambda,
    HttpApi) must carry ``treadmill:deployment_id=test``. CDK's tag aspect
    propagates the stack-level tag through any CFN resource whose schema
    declares a ``Tags`` property; this test is the regression net for
    that propagation."""
    template = _template().to_json()
    resources = template["Resources"]
    taggable_types = {
        "AWS::SQS::Queue",
        "AWS::Lambda::Function",
        "AWS::ApiGatewayV2::Api",
    }

    seen_types: set[str] = set()
    for logical_id, resource in resources.items():
        rtype = resource["Type"]
        if rtype not in taggable_types:
            continue
        props = resource.get("Properties", {})
        assert _resource_has_tag(
            props, "treadmill:deployment_id", "test",
        ), (
            f"resource {logical_id} ({rtype}) missing tag "
            f"treadmill:deployment_id=test; got Tags={props.get('Tags')!r}"
        )
        seen_types.add(rtype)

    # Sanity: the template must contain at least one of each taggable
    # type (queue + DLQ collapse into AWS::SQS::Queue; one Lambda; one
    # HttpApi). All three CFN resource types should be present.
    assert seen_types == taggable_types, (
        f"expected to assert tags on each of {taggable_types}; "
        f"only saw {seen_types}"
    )
