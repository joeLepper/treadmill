"""Observability construct — CloudWatch billing alarm + alarm SNS topic.

Provisions a per-deployment guardrail against the failure mode where a
misconfigured Lambda loop, runaway SQS retention, or accidental cross-
region usage spikes the operator's AWS bill. The alarm watches the
AWS-billing ``EstimatedCharges`` metric and publishes to an SNS topic
when the monthly charges cross ``billing_alarm_threshold_usd`` (default
$10, per ADR-0016's ~$2/month dev-local budget — $10 leaves ~5x
headroom before paging).

The SNS topic exists so the operator can subscribe their email via
``aws sns subscribe`` post-deploy. We deliberately do NOT auto-subscribe
at v0: SNS email subscription requires confirming a delivered email,
which is friction we don't want to bake into ``cdk deploy``. The runbook
documents the manual ``aws sns subscribe --protocol email`` step.

**Regional constraint — ``AWS/Billing``.**  The ``AWS/Billing``
namespace is only published in ``us-east-1``. Per ADR-0016 the canonical
dev-local region IS ``us-east-1``, so this construct is correct for the
v0 deployment. If the stack is ever deployed to a different region the
CloudWatch alarm resource will still synthesize and create cleanly —
CloudWatch lets you create alarms against unavailable metrics, they
simply never enter ALARM state because no datapoints arrive. We don't
over-engineer for the non-canonical case here; a future ADR can revisit
if multi-region dev-local becomes a thing.
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
)
from constructs import Construct


class ObservabilityConstruct(Construct):
    """Per-deployment CloudWatch billing alarm + alarm-notification SNS topic.

    Args:
        scope: Parent CDK scope (typically the ``TreadmillCloudLite`` stack).
        construct_id: CDK logical-id namespace for this construct.
        deployment_id: Lowercase alphanumeric slug (regex
            ``^[a-z][a-z0-9]{0,29}$``) suffixing the topic + alarm names.
        billing_alarm_threshold_usd: USD threshold for the monthly-billing
            alarm. Default $10/month; ADR-0016's dev-local budget is
            ~$2/month so $10 is ~5x headroom before paging.

    Exposes:
        billing_alarms_topic: The SNS ``Topic`` the alarm publishes to.
            Operator subscribes their email post-deploy via
            ``aws sns subscribe``.
        billing_alarm: The CloudWatch ``Alarm`` watching ``EstimatedCharges``.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_id: str,
        billing_alarm_threshold_usd: float = 10.0,
    ) -> None:
        super().__init__(scope, construct_id)

        prefix = f"treadmill-{deployment_id}"

        # ── SNS topic for alarm notifications ────────────────────────────────
        # No subscriptions are created at synth time — the operator runs
        # ``aws sns subscribe --topic-arn <arn> --protocol email
        # --notification-endpoint <email>`` post-deploy and confirms the
        # subscription email. Auto-subscribing would require the email
        # address at synth time, which we don't have in CDK context.
        self.billing_alarms_topic = sns.Topic(
            self,
            "BillingAlarmsTopic",
            topic_name=f"{prefix}-billing-alarms",
        )

        # ── Billing metric ───────────────────────────────────────────────────
        # AWS/Billing publishes ``EstimatedCharges`` for the linked account.
        # The metric is reported in ~6-hour cycles, so a 6-hour period is
        # the tightest meaningful evaluation window. ``Maximum`` statistic
        # captures the most recent (and largest, since charges are
        # cumulative within the billing month) datapoint in the window.
        # Dimensions: ``Currency=USD`` — without this the alarm would
        # never see datapoints (the metric is always reported with a
        # currency dimension; there is no "all currencies" rollup).
        billing_metric = cloudwatch.Metric(
            namespace="AWS/Billing",
            metric_name="EstimatedCharges",
            dimensions_map={"Currency": "USD"},
            statistic="Maximum",
            period=Duration.seconds(21600),  # 6 hours
        )

        # ── Alarm ────────────────────────────────────────────────────────────
        self.billing_alarm = cloudwatch.Alarm(
            self,
            "BillingAlarm",
            alarm_name=f"{prefix}-monthly-billing-over-threshold",
            alarm_description=(
                f"Treadmill {deployment_id}: monthly EstimatedCharges "
                f"crossed ${billing_alarm_threshold_usd:g} USD. "
                "Investigate before the credit-card statement lands."
            ),
            metric=billing_metric,
            threshold=billing_alarm_threshold_usd,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
            # ``MISSING`` (default) is correct here: outside us-east-1 the
            # metric simply has no datapoints, and we don't want a flapping
            # alarm in that case.
            treat_missing_data=cloudwatch.TreatMissingData.MISSING,
        )
        self.billing_alarm.add_alarm_action(
            cw_actions.SnsAction(self.billing_alarms_topic),
        )

        # ── CloudFormation outputs ───────────────────────────────────────────
        # ``BillingAlarmsTopicArn`` so the operator can
        # ``aws sns subscribe --topic-arn $(yq .observability.billing_alarms_topic_arn ...)``.
        CfnOutput(
            self,
            "BillingAlarmsTopicArn",
            value=self.billing_alarms_topic.topic_arn,
            description=(
                "ARN of the SNS topic the billing alarm publishes to. "
                "Subscribe your email post-deploy via "
                "`aws sns subscribe --topic-arn <arn> --protocol email "
                "--notification-endpoint <you@example.com>`."
            ),
        )
        # ``BillingAlarmName`` so the operator can verify in the CloudWatch
        # console or query state via ``aws cloudwatch describe-alarms``.
        CfnOutput(
            self,
            "BillingAlarmName",
            value=self.billing_alarm.alarm_name,
            description=(
                "Name of the CloudWatch alarm watching the monthly "
                "EstimatedCharges metric. Verify via "
                "`aws cloudwatch describe-alarms --alarm-names <name>`."
            ),
        )
