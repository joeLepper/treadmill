"""Assertions on the synthesized ``ObservabilityConstruct``.

The construct (per Phase B.5 of the Week-4 plan) provisions:

- An SNS topic ``treadmill-<deployment_id>-billing-alarms`` for alarm
  notifications (operator subscribes their email post-deploy).
- A CloudWatch alarm
  ``treadmill-<deployment_id>-monthly-billing-over-threshold`` on the
  ``AWS/Billing::EstimatedCharges`` metric with ``Currency=USD``,
  6-hour period, ``Maximum`` statistic, ``GreaterThanThreshold``
  comparison, default $10 threshold.
- The alarm's ``alarm_actions`` include the SNS topic ARN.
- CloudFormation outputs: ``BillingAlarmsTopicArn``, ``BillingAlarmName``.
- The ``treadmill:deployment_id=<deployment_id>`` tag on both the SNS
  topic and the alarm.

Tests instantiate the construct inside a throwaway stack (so we can
also assert the parent-applied tag propagates and the CFN outputs land
on the stack) and walk the synthesized template.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions

from treadmill_infra.constructs.observability import ObservabilityConstruct


def _synth(
    deployment_id: str = "test",
    *,
    threshold: float | None = None,
) -> assertions.Template:
    """Synthesize a one-construct stack and return the assertion template."""
    app = cdk.App()
    stack = cdk.Stack(app, "TestStack")
    cdk.Tags.of(stack).add("treadmill:deployment_id", deployment_id)
    kwargs: dict = {"deployment_id": deployment_id}
    if threshold is not None:
        kwargs["billing_alarm_threshold_usd"] = threshold
    ObservabilityConstruct(stack, "Observability", **kwargs)
    return assertions.Template.from_stack(stack)


# ── SNS topic ────────────────────────────────────────────────────────────────


def test_billing_alarms_topic_with_deployment_suffix():
    template = _synth()
    template.has_resource_properties(
        "AWS::SNS::Topic",
        {"TopicName": "treadmill-test-billing-alarms"},
    )


def test_only_one_sns_topic():
    """The construct provisions exactly the alarms topic — nothing else."""
    template = _synth()
    template.resource_count_is("AWS::SNS::Topic", 1)


# ── CloudWatch alarm ─────────────────────────────────────────────────────────


def test_billing_alarm_with_deployment_suffix():
    template = _synth()
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"AlarmName": "treadmill-test-monthly-billing-over-threshold"},
    )


def test_billing_alarm_metric_namespace_and_name():
    """Alarm watches ``AWS/Billing::EstimatedCharges``."""
    template = _synth()
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Namespace": "AWS/Billing",
            "MetricName": "EstimatedCharges",
        },
    )


def test_billing_alarm_dimension_currency_usd():
    """Dimension ``Currency=USD`` is required — without it the metric
    has no datapoints (AWS/Billing always publishes with the currency
    dimension; there is no all-currencies rollup)."""
    template = _synth()
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Dimensions": [{"Name": "Currency", "Value": "USD"}],
        },
    )


def test_billing_alarm_period_six_hours():
    """6 hours = 21600 seconds; AWS/Billing publishes ~every 6h so a
    tighter period would just observe the same datapoint repeatedly."""
    template = _synth()
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"Period": 21600},
    )


def test_billing_alarm_statistic_maximum():
    template = _synth()
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"Statistic": "Maximum"},
    )


def test_billing_alarm_default_threshold_is_ten():
    template = _synth()
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"Threshold": 10},
    )


def test_billing_alarm_comparison_operator_greater_than():
    template = _synth()
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"ComparisonOperator": "GreaterThanThreshold"},
    )


def test_billing_alarm_evaluation_periods_one():
    template = _synth()
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"EvaluationPeriods": 1},
    )


def test_billing_alarm_action_targets_sns_topic():
    """``AlarmActions`` is a list containing a ref to the SNS topic. CDK
    synthesizes this as ``{"Ref": "<TopicLogicalId>"}`` (the topic ARN
    is the topic's ``Ref`` value). We confirm by walking the template:
    find the alarm, extract its ``AlarmActions``, and confirm it
    references the same logical id as the topic."""
    template = _synth().to_json()
    resources = template["Resources"]

    topic_lids = [
        lid for lid, r in resources.items() if r["Type"] == "AWS::SNS::Topic"
    ]
    alarm_lids = [
        lid
        for lid, r in resources.items()
        if r["Type"] == "AWS::CloudWatch::Alarm"
    ]
    assert len(topic_lids) == 1, f"expected 1 SNS topic, got {topic_lids}"
    assert len(alarm_lids) == 1, f"expected 1 alarm, got {alarm_lids}"
    topic_lid = topic_lids[0]
    alarm_lid = alarm_lids[0]

    actions = resources[alarm_lid]["Properties"].get("AlarmActions", [])
    assert len(actions) == 1, (
        f"alarm {alarm_lid} expected exactly 1 AlarmAction, got {actions}"
    )
    # CFN intrinsic: the alarm action is a Ref to the SNS topic (the
    # topic's Ref evaluates to its ARN at deploy time).
    action = actions[0]
    assert action == {"Ref": topic_lid}, (
        f"alarm AlarmAction {action} does not reference SNS topic "
        f"logical id {topic_lid}"
    )


# ── CloudFormation outputs ───────────────────────────────────────────────────


def test_cfn_output_billing_alarms_topic_arn_exists():
    """CDK prefixes output logical ids with the construct path
    (``Observability``) and suffixes them with an 8-char hash; we match
    on the substring rather than the full hash-stable id."""
    template = _synth()
    outputs = template.to_json().get("Outputs", {})
    matching = [k for k in outputs if "BillingAlarmsTopicArn" in k]
    assert matching, (
        "expected an Output whose logical id contains "
        f"'BillingAlarmsTopicArn'; got {list(outputs.keys())}"
    )
    # Description matches the documented `aws sns subscribe` guidance.
    template.has_output(
        matching[0],
        {
            "Description": assertions.Match.string_like_regexp(
                ".*ARN of the SNS topic the billing alarm publishes to.*",
            ),
        },
    )


def test_cfn_output_billing_alarm_name_exists():
    template = _synth()
    outputs = template.to_json().get("Outputs", {})
    matching = [
        k
        for k in outputs
        if "BillingAlarmName" in k and "BillingAlarmsTopicArn" not in k
    ]
    assert matching, (
        "expected an Output whose logical id contains 'BillingAlarmName'; "
        f"got {list(outputs.keys())}"
    )
    # Value resolves to the alarm name at deploy time. CFN renders this
    # as ``{"Ref": "<AlarmLogicalId>"}`` because ``Alarm.alarm_name`` is
    # a CDK token, not a literal — for an ``AWS::CloudWatch::Alarm``,
    # ``Ref`` evaluates to the alarm's name. We assert the shape; the
    # alarm name itself is asserted directly on the alarm resource by
    # ``test_billing_alarm_with_deployment_suffix``.
    output = outputs[matching[0]]
    template_json = template.to_json()
    alarm_lids = [
        lid
        for lid, r in template_json["Resources"].items()
        if r["Type"] == "AWS::CloudWatch::Alarm"
    ]
    assert len(alarm_lids) == 1
    assert output["Value"] == {"Ref": alarm_lids[0]}, (
        f"BillingAlarmName output value {output['Value']!r} does not "
        f"reference alarm logical id {alarm_lids[0]!r}"
    )


# ── Tag propagation (deployment_id) ──────────────────────────────────────────


def _tag_pairs(props: dict) -> list[tuple[str, str]]:
    return [(t["Key"], t["Value"]) for t in props.get("Tags", [])]


def test_deployment_id_tag_on_sns_topic():
    """ADR-0016 §"Cost attribution backstop": the alarms topic carries
    ``treadmill:deployment_id=<deployment_id>`` (inherited from the
    stack-level ``Tags.of(stack).add(...)``)."""
    template = _synth().to_json()
    topics = [
        r
        for r in template["Resources"].values()
        if r["Type"] == "AWS::SNS::Topic"
    ]
    assert len(topics) == 1
    tags = _tag_pairs(topics[0].get("Properties", {}))
    assert ("treadmill:deployment_id", "test") in tags, (
        f"SNS topic missing treadmill:deployment_id tag; got {tags}"
    )


def test_deployment_id_tag_on_alarm():
    """Same regression net for the alarm. CloudWatch alarms support
    Tags; CDK's stack-level ``Tags.of`` cascades into them."""
    template = _synth().to_json()
    alarms = [
        r
        for r in template["Resources"].values()
        if r["Type"] == "AWS::CloudWatch::Alarm"
    ]
    assert len(alarms) == 1
    tags = _tag_pairs(alarms[0].get("Properties", {}))
    assert ("treadmill:deployment_id", "test") in tags, (
        f"Alarm missing treadmill:deployment_id tag; got {tags}"
    )


# ── Threshold override ───────────────────────────────────────────────────────


def test_threshold_override_via_constructor():
    """``billing_alarm_threshold_usd=50.0`` produces an alarm with
    threshold 50 — the kwarg flows through to CFN."""
    template = _synth(threshold=50.0)
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"Threshold": 50},
    )
