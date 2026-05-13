"""Assertions on the synthesized ``SecretsConstruct`` template.

Per ADR-0016 + Week-4 plan B.2, ``SecretsConstruct`` provisions three
deployment-scoped Secrets Manager entries (GitHub webhook secret,
GitHub PAT, worker AWS credentials) plus an API IAM user and API
credentials secret (per ADR-0023). Tests check:

1. Each of the four secrets exists with its deployment-suffixed physical name.
2. Each secret has ``DeletionPolicy: Delete`` (so ``cdk destroy``
   issues a real delete, not an orphan). Skipping Secrets Manager's
   7-30 day recovery window is an operator step run *before*
   ``cdk destroy`` (see the stack-deletion runbook); not expressible as
   a CFN property.
3. The API IAM user exists with the correct name and has an inline policy.
4. The API IAM policy has the four resource scopes and ~7 actions per ADR-0023.
5. Five ``CfnOutput`` exports exist with the right logical IDs and
   reference the secret names and API user ARN that ``treadmill-local init`` reads.
6. The construct's ``treadmill:deployment_id`` tag (inherited from the
   parent stack via ``Tags.of(scope)``) lands on each secret resource.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from treadmill_infra.constructs import SecretsConstruct


def _stack_with_secrets(deployment_id: str = "test") -> cdk.Stack:
    """Build a minimal test stack with a SecretsConstruct.

    Mirrors how ``TreadmillCloudLite`` composes the construct: stack
    applies the ``treadmill:deployment_id`` tag at its scope, the
    construct provisions resources underneath.
    """
    app = cdk.App()
    stack = cdk.Stack(app, "TestStack")
    cdk.Tags.of(stack).add("treadmill:deployment_id", deployment_id)
    SecretsConstruct(stack, "Secrets", deployment_id=deployment_id)
    return stack


def _template(deployment_id: str = "test") -> assertions.Template:
    return assertions.Template.from_stack(_stack_with_secrets(deployment_id))


# ── Secret resources with deployment-suffixed names ───────────────────────────


@pytest.mark.parametrize(
    "secret_name",
    [
        "treadmill-test/github-webhook-secret",
        "treadmill-test/github-pat",
        "treadmill-test/worker-aws-credentials",
        "treadmill-test/api-aws-credentials",
    ],
)
def test_each_secret_exists_with_expected_name(secret_name: str):
    template = _template()
    template.has_resource_properties(
        "AWS::SecretsManager::Secret",
        {"Name": secret_name},
    )


def test_exactly_four_secrets():
    """Phase B.2 + ADR-0023 provisions exactly four secrets (GitHub webhook,
    GitHub PAT, worker AWS credentials, API AWS credentials); future ADRs
    that add more (e.g. rotation tokens) should update this assertion deliberately."""
    template = _template()
    template.resource_count_is("AWS::SecretsManager::Secret", 4)


def test_secret_names_use_personal_deployment_id_correctly():
    """The deployment-suffix substitution works for a non-``test`` id."""
    template = _template(deployment_id="personal")
    for secret_name in (
        "treadmill-personal/github-webhook-secret",
        "treadmill-personal/github-pat",
        "treadmill-personal/worker-aws-credentials",
        "treadmill-personal/api-aws-credentials",
    ):
        template.has_resource_properties(
            "AWS::SecretsManager::Secret",
            {"Name": secret_name},
        )


# ── Removal policy (DeletionPolicy=Delete) ────────────────────────────────────


def test_every_secret_has_delete_deletion_policy():
    """``removal_policy=DESTROY`` on the L2 ``Secret`` synthesizes as
    ``DeletionPolicy: Delete`` (and ``UpdateReplacePolicy: Delete``) on
    the CFN resource. Without this, ``cdk destroy`` orphans the secret."""
    template = _template().to_json()
    secrets = {
        lid: res
        for lid, res in template["Resources"].items()
        if res["Type"] == "AWS::SecretsManager::Secret"
    }
    assert len(secrets) == 4, f"expected 4 secrets, found {len(secrets)}"
    for logical_id, resource in secrets.items():
        assert resource.get("DeletionPolicy") == "Delete", (
            f"{logical_id}: expected DeletionPolicy=Delete, "
            f"got {resource.get('DeletionPolicy')!r}"
        )
        assert resource.get("UpdateReplacePolicy") == "Delete", (
            f"{logical_id}: expected UpdateReplacePolicy=Delete, "
            f"got {resource.get('UpdateReplacePolicy')!r}"
        )


# ── No ForceDeleteWithoutRecovery property ────────────────────────────────────


def test_no_secret_sets_force_delete_without_recovery():
    """CloudFormation does NOT support ``ForceDeleteWithoutRecovery`` as
    a property on ``AWS::SecretsManager::Secret`` — it's a parameter on
    the ``DeleteSecret`` API call only. A prior attempt to set it via
    CFN escape-hatch caused ``cdk deploy`` to fail with "Unsupported
    property [ForceDeleteWithoutRecovery]". Skipping the recovery window
    is an operator step run *before* ``cdk destroy`` per the runbook.
    Regression test: never re-introduce the property override."""
    template = _template().to_json()
    secrets = {
        lid: res
        for lid, res in template["Resources"].items()
        if res["Type"] == "AWS::SecretsManager::Secret"
    }
    assert len(secrets) == 4
    for logical_id, resource in secrets.items():
        assert "ForceDeleteWithoutRecovery" not in resource["Properties"], (
            f"{logical_id}: ForceDeleteWithoutRecovery is not a valid CFN "
            f"property on AWS::SecretsManager::Secret; runbook handles "
            f"the recovery-window skip via the delete-secret API call."
        )


# ── CloudFormation outputs ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("output_logical_id_substring", "expected_value"),
    [
        ("GithubWebhookSecretName", "treadmill-test/github-webhook-secret"),
        ("GithubPatSecretName", "treadmill-test/github-pat"),
        (
            "WorkerAwsCredentialsSecretName",
            "treadmill-test/worker-aws-credentials",
        ),
        ("ApiAwsCredentialsSecretName", "treadmill-test/api-aws-credentials"),
    ],
)
def test_cfn_output_exists_with_expected_value(
    output_logical_id_substring: str,
    expected_value: str,
):
    """Each output's value is the literal secret name or resource ARN.

    We deliberately set the CfnOutput value to the same string we passed
    as ``secret_name`` (NOT ``secret.secret_name``, which is a token
    that synthesizes into a complex ``Fn::Join`` of ``Fn::Split``
    expressions reconstructing the name from the ARN at deploy time).
    The literal form is what ``treadmill-local init`` reads.
    """
    template = _template().to_json()
    outputs = template.get("Outputs", {})
    matching = {
        lid: o
        for lid, o in outputs.items()
        if output_logical_id_substring in lid
    }
    assert matching, (
        f"no output found containing {output_logical_id_substring!r}; "
        f"outputs: {list(outputs)}"
    )
    [(_lid, output)] = matching.items()
    assert output.get("Value") == expected_value, (
        f"expected output {output_logical_id_substring} value to be "
        f"{expected_value!r}; got {output.get('Value')!r}"
    )


def test_exactly_five_outputs():
    """Phase B.2 + ADR-0023 emits five outputs (three secret names, one API IAM
    user ARN, one API secret name); drift here would mean a stray output the
    YAML schema in ADR-0016 + ADR-0023 doesn't know about."""
    template = _template().to_json()
    secret_outputs = [
        lid
        for lid in template.get("Outputs", {})
        if any(
            key in lid
            for key in (
                "GithubWebhookSecretName",
                "GithubPatSecretName",
                "WorkerAwsCredentialsSecretName",
                "ApiIamUserArn",
                "ApiAwsCredentialsSecretName",
            )
        )
    ]
    assert len(secret_outputs) == 5, (
        f"expected 5 outputs, got {secret_outputs}"
    )


# ── API IAM user + policy ────────────────────────────────────────────────────


def test_api_iam_user_exists_with_correct_name():
    """Per ADR-0023: the API gets a dedicated IAM user."""
    template = _template()
    template.has_resource_properties(
        "AWS::IAM::User",
        {"UserName": "treadmill-test-api"},
    )


def test_api_iam_user_arn_in_outputs():
    """Per ADR-0023: the API IAM user's ARN is exported as a CFN output
    for ``treadmill-local init`` to use."""
    template = _template().to_json()
    outputs = template.get("Outputs", {})
    api_arn_outputs = {
        lid: o for lid, o in outputs.items() if "ApiIamUserArn" in lid
    }
    assert api_arn_outputs, (
        f"no output found containing 'ApiIamUserArn'; outputs: {list(outputs)}"
    )
    [(_lid, output)] = api_arn_outputs.items()
    # The value should be a reference (likely Fn::GetAtt) to the user ARN.
    assert "Value" in output, f"output missing Value property"
    # The value will be a Fn::GetAtt referencing the user resource.
    value = output.get("Value")
    assert value is not None, "ApiIamUserArn output has no value"


def test_api_iam_user_has_inline_policy():
    """Per ADR-0023: the API IAM user has an inline policy granting
    narrowly scoped access to SQS, SNS, and Secrets Manager."""
    template = _template().to_json()
    policies = [
        res
        for res in template["Resources"].values()
        if res["Type"] == "AWS::IAM::Policy"
    ]
    assert len(policies) == 1, f"expected 1 IAM policy, found {len(policies)}"
    policy = policies[0]
    # The policy should be attached to the API user.
    assert "Users" in policy["Properties"], (
        f"policy missing Users property; got {policy['Properties']}"
    )
    users = policy["Properties"]["Users"]
    assert len(users) == 1, f"expected 1 user, got {users}"


def test_api_iam_policy_has_required_statements():
    """Per ADR-0023 §'IAM scope': the policy has statements for SQS
    (coordination + webhook-inbox), SQS (work.fifo), SNS (events),
    and Secrets Manager (github-webhook-secret)."""
    template = _template().to_json()
    policies = [
        res
        for res in template["Resources"].values()
        if res["Type"] == "AWS::IAM::Policy"
    ]
    assert len(policies) == 1
    policy = policies[0]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    assert len(statements) == 4, (
        f"expected 4 statements (SQS coordination/inbox, SQS work, SNS, SM), "
        f"got {len(statements)}"
    )

    # Check actions in the policy (order might vary).
    all_actions = set()
    for stmt in statements:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        all_actions.update(actions)

    expected_actions = {
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:SendMessage",
        "sns:Publish",
        "secretsmanager:GetSecretValue",
    }
    assert all_actions == expected_actions, (
        f"policy actions mismatch; expected {expected_actions}, got {all_actions}"
    )

    # Verify resource ARNs include the queues, topic, and secret. CDK
    # cross-resource ARN refs synthesize as token dicts (``Fn::Sub`` /
    # ``Fn::Join`` / ``Fn::GetAtt``) rather than literal strings, so we
    # JSON-encode each Resource for the substring check rather than
    # treating it as a string.
    import json as _json
    resource_blobs: list[str] = []
    for stmt in statements:
        resources = stmt.get("Resource", [])
        if isinstance(resources, (str, dict)):
            resources = [resources]
        for r in resources:
            resource_blobs.append(r if isinstance(r, str) else _json.dumps(r))

    # Check that key resource patterns are present.
    assert any("coordination" in r for r in resource_blobs), (
        "policy missing coordination queue resource"
    )
    assert any("webhook-inbox" in r for r in resource_blobs), (
        "policy missing webhook-inbox queue resource"
    )
    assert any("work.fifo" in r for r in resource_blobs), (
        "policy missing work.fifo queue resource"
    )
    assert any("events" in r for r in resource_blobs), (
        "policy missing events topic resource"
    )
    assert any("github-webhook-secret" in r for r in resource_blobs), (
        "policy missing github-webhook-secret resource"
    )


# ── Tagging ───────────────────────────────────────────────────────────────────


def test_every_secret_carries_deployment_id_tag():
    """ADR-0016 §"Cost attribution backstop": the
    ``treadmill:deployment_id`` tag applied at the stack scope must
    propagate through CDK's tag aspect onto every Secrets Manager
    resource."""
    template = _template().to_json()
    secrets = [
        res
        for res in template["Resources"].values()
        if res["Type"] == "AWS::SecretsManager::Secret"
    ]
    assert len(secrets) == 4
    expected = {"Key": "treadmill:deployment_id", "Value": "test"}
    for resource in secrets:
        tags = resource["Properties"].get("Tags", [])
        assert expected in tags, (
            f"secret missing tag {expected}; got {tags}"
        )
