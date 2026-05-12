"""Assertions on the synthesized ``SecretsConstruct`` template.

Per ADR-0016 + Week-4 plan B.2, ``SecretsConstruct`` provisions three
deployment-scoped Secrets Manager entries (GitHub webhook secret,
GitHub PAT, worker AWS credentials). Tests check:

1. Each secret exists with its deployment-suffixed physical name.
2. Each secret has ``DeletionPolicy: Delete`` (so ``cdk destroy``
   issues a real delete, not an orphan). Skipping Secrets Manager's
   7-30 day recovery window is an operator step run *before*
   ``cdk destroy`` (see the stack-deletion runbook); not expressible as
   a CFN property.
3. Three ``CfnOutput`` exports exist with the right logical IDs and
   reference the secret names that ``treadmill-local init`` reads.
4. The construct's ``treadmill:deployment_id`` tag (inherited from the
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
    ],
)
def test_each_secret_exists_with_expected_name(secret_name: str):
    template = _template()
    template.has_resource_properties(
        "AWS::SecretsManager::Secret",
        {"Name": secret_name},
    )


def test_exactly_three_secrets():
    """Phase B.2 provisions exactly three secrets; future ADRs that add
    more (e.g. rotation tokens) should update this assertion deliberately."""
    template = _template()
    template.resource_count_is("AWS::SecretsManager::Secret", 3)


def test_secret_names_use_personal_deployment_id_correctly():
    """The deployment-suffix substitution works for a non-``test`` id."""
    template = _template(deployment_id="personal")
    for secret_name in (
        "treadmill-personal/github-webhook-secret",
        "treadmill-personal/github-pat",
        "treadmill-personal/worker-aws-credentials",
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
    assert len(secrets) == 3, f"expected 3 secrets, found {len(secrets)}"
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
    assert len(secrets) == 3
    for logical_id, resource in secrets.items():
        assert "ForceDeleteWithoutRecovery" not in resource["Properties"], (
            f"{logical_id}: ForceDeleteWithoutRecovery is not a valid CFN "
            f"property on AWS::SecretsManager::Secret; runbook handles "
            f"the recovery-window skip via the delete-secret API call."
        )


# ── CloudFormation outputs ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("output_logical_id_substring", "expected_secret_name"),
    [
        ("GithubWebhookSecretName", "treadmill-test/github-webhook-secret"),
        ("GithubPatSecretName", "treadmill-test/github-pat"),
        (
            "WorkerAwsCredentialsSecretName",
            "treadmill-test/worker-aws-credentials",
        ),
    ],
)
def test_cfn_output_exists_with_expected_secret_name(
    output_logical_id_substring: str,
    expected_secret_name: str,
):
    """Each output's value is the literal secret name.

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
    assert output.get("Value") == expected_secret_name, (
        f"expected output {output_logical_id_substring} value to be "
        f"{expected_secret_name!r}; got {output.get('Value')!r}"
    )


def test_exactly_three_outputs():
    """Phase B.2 emits three outputs; drift here would mean a stray
    output the YAML schema in ADR-0016 doesn't know about."""
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
            )
        )
    ]
    assert len(secret_outputs) == 3, (
        f"expected 3 secret-name outputs, got {secret_outputs}"
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
    assert len(secrets) == 3
    expected = {"Key": "treadmill:deployment_id", "Value": "test"}
    for resource in secrets:
        tags = resource["Properties"].get("Tags", [])
        assert expected in tags, (
            f"secret missing tag {expected}; got {tags}"
        )
