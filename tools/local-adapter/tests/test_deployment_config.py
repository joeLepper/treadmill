"""Tests for ``treadmill_local.deployment_config`` + the ``init`` CLI command.

Covers:

- ``read_stack_outputs`` against a mocked CloudFormation client.
- ``build_deployment_config`` against synthetic outputs that mimic CDK's
  hash-suffixed logical id pattern.
- ``write_deployment_yaml`` round-trips through tmp_path.
- Missing-output handling raises ``KeyError`` naming the missing suffix.
- End-to-end CLI invocation with ``moto``-mocked AWS.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
import yaml
from moto import mock_aws
from typer.testing import CliRunner

from treadmill_local.cli import app
from treadmill_local.deployment_config import (
    build_deployment_config,
    load_deployment_yaml,
    read_stack_outputs,
    write_deployment_yaml,
)


runner = CliRunner()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def synthetic_outputs() -> dict[str, str]:
    """Mimic CDK's hash-suffixed logical ids for every CFN output.

    The 8-char hex hashes here are illustrative (one per output); the
    suffix-match logic in ``deployment_config`` must tolerate them.
    """
    return {
        # Messaging
        "MessagingEventsTopicArnA1B2C3D4": (
            "arn:aws:sns:us-east-1:111111111111:treadmill-test-events"
        ),
        "MessagingEventsQueueUrlE5F6A7B8": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-coordination"
        ),
        "MessagingWorkQueueUrlC9D0E1F2": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-work.fifo"
        ),
        # Webhook receiver
        "WebhookReceiverWebhookApiUrl51C59AB0": (
            "https://abc123.execute-api.us-east-1.amazonaws.com"
        ),
        "WebhookReceiverWebhookInboxQueueUrl1234ABCD": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-webhook-inbox"
        ),
        "WebhookReceiverWebhookInboxDlqUrlABCD1234": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-webhook-inbox-dlq"
        ),
        # Deploy events
        "DeployEventsQueueUrl12345678": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-deploy-events"
        ),
        "DeployEventsDlqUrlABCDEFGH": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-deploy-events-dlq"
        ),
        # Secrets
        "SecretsGithubWebhookSecretName11112222": (
            "treadmill-test/github-webhook-secret"
        ),
        "SecretsGithubPatSecretName33334444": (
            "treadmill-test/github-pat"
        ),
        "SecretsWorkerAwsCredentialsSecretName55556666": (
            "treadmill-test/worker-aws-credentials"
        ),
        "SecretsApiAwsCredentialsSecretName77778888": (
            "treadmill-test/api-aws-credentials"
        ),
        # Observability (present but not consumed by build_deployment_config —
        # ADR-0016's YAML schema doesn't include the alarm topic; the test
        # asserts the extra outputs are ignored, not raised on).
        "ObservabilityBillingAlarmsTopicArnDEADBEEF": (
            "arn:aws:sns:us-east-1:111111111111:treadmill-test-billing-alarms"
        ),
        "ObservabilityBillingAlarmNameCAFEBABE": (
            "treadmill-test-monthly-billing-over-threshold"
        ),
    }


# ── read_stack_outputs ────────────────────────────────────────────────────────


def test_read_stack_outputs_returns_key_value_dict():
    """Outputs in the describe_stacks response come back as a flat dict."""
    fake_response = {
        "Stacks": [
            {
                "StackName": "TreadmillTestCloudLite",
                "Outputs": [
                    {"OutputKey": "WebhookApiUrl123", "OutputValue": "https://api.example"},
                    {"OutputKey": "GithubPatSecretName456", "OutputValue": "secret-name"},
                ],
            }
        ]
    }
    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.return_value = fake_response
    # exceptions.ClientError is accessed for narrow except; assign a stub.
    mock_cfn.exceptions.ClientError = Exception

    mock_session = MagicMock()
    mock_session.client.return_value = mock_cfn

    with patch("treadmill_local.deployment_config.boto3.Session", return_value=mock_session):
        outputs = read_stack_outputs(
            "TreadmillTestCloudLite", profile="treadmill-test", region="us-east-1",
        )
    assert outputs == {
        "WebhookApiUrl123": "https://api.example",
        "GithubPatSecretName456": "secret-name",
    }
    mock_cfn.describe_stacks.assert_called_once_with(StackName="TreadmillTestCloudLite")


def test_read_stack_outputs_missing_stack_raises_clear_error():
    """A nonexistent stack surfaces as a ``ValueError`` naming the stack."""
    class _ClientError(Exception):
        def __init__(self):
            self.response = {
                "Error": {
                    "Code": "ValidationError",
                    "Message": (
                        "Stack with id TreadmillTestCloudLite does not exist"
                    ),
                }
            }
            super().__init__("ValidationError")

    mock_cfn = MagicMock()
    mock_cfn.exceptions.ClientError = _ClientError
    mock_cfn.describe_stacks.side_effect = _ClientError()

    mock_session = MagicMock()
    mock_session.client.return_value = mock_cfn

    with patch("treadmill_local.deployment_config.boto3.Session", return_value=mock_session):
        with pytest.raises(ValueError, match="TreadmillTestCloudLite"):
            read_stack_outputs(
                "TreadmillTestCloudLite", profile="treadmill-test", region="us-east-1",
            )


@mock_aws
def test_read_stack_outputs_against_moto(monkeypatch):
    """End-to-end against moto: create a real stack with outputs and read them back."""
    # Seed env-var credentials so boto3.Session(profile_name=None) does not
    # try to load ``~/.aws/credentials``. moto intercepts API calls
    # regardless of which credentials are used.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    cfn = boto3.client("cloudformation", region_name="us-east-1")
    template = {
        "Resources": {
            "DummyTopic": {
                "Type": "AWS::SNS::Topic",
                "Properties": {"TopicName": "dummy"},
            }
        },
        "Outputs": {
            "WebhookApiUrlABCD1234": {
                "Value": "https://abc.execute-api.us-east-1.amazonaws.com",
            },
            "GithubPatSecretName56789ABC": {
                "Value": "treadmill-test/github-pat",
            },
        },
    }
    import json as _json
    cfn.create_stack(
        StackName="TreadmillTestCloudLite",
        TemplateBody=_json.dumps(template),
    )

    outputs = read_stack_outputs(
        "TreadmillTestCloudLite", profile=None, region="us-east-1",
    )
    assert outputs["WebhookApiUrlABCD1234"] == (
        "https://abc.execute-api.us-east-1.amazonaws.com"
    )
    assert outputs["GithubPatSecretName56789ABC"] == (
        "treadmill-test/github-pat"
    )


# ── build_deployment_config ───────────────────────────────────────────────────


def test_build_deployment_config_populates_every_yaml_key(synthetic_outputs):
    """Each contract key in ADR-0016's YAML schema is populated correctly."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )

    # Top-level
    assert config["deployment_id"] == "test"
    assert config["deployment_mode"] == "dev_local"
    assert config["aws_profile"] == "treadmill-test"
    assert config["aws_region"] == "us-east-1"
    assert config["aws_account_id"] == "111111111111"
    assert isinstance(config["aws_account_id"], str)

    # aws block — substring matching pulled the right values out of
    # hash-suffixed keys. The synthetic outputs above do NOT include
    # ``TreadmillObservabilityStack`` CFN outputs (the normal dev_local
    # case: that stack is fully_remote-only), so the dev-local defaults
    # for ``observability_collector_endpoint``, ``observability_grafana_host``,
    # and ``observability_grafana_port`` are stamped in.
    assert config["aws"] == {
        "events_topic_arn": (
            "arn:aws:sns:us-east-1:111111111111:treadmill-test-events"
        ),
        "events_queue_url": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-coordination"
        ),
        "work_queue_url": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-work.fifo"
        ),
        "webhook_inbox_queue_url": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-webhook-inbox"
        ),
        "webhook_inbox_dlq_url": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-webhook-inbox-dlq"
        ),
        "webhook_api_url": "https://abc123.execute-api.us-east-1.amazonaws.com",
        "deploy_events_queue_url": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-deploy-events"
        ),
        "deploy_events_dlq_url": (
            "https://sqs.us-east-1.amazonaws.com/111111111111/"
            "treadmill-test-deploy-events-dlq"
        ),
        "observability_collector_endpoint": (
            "http://treadmill-otel-collector:4318"
        ),
        "observability_grafana_host": "127.0.0.1",
        "observability_grafana_port": 3001,
    }

    # secrets block
    assert config["secrets"] == {
        "github_webhook_secret_name": "treadmill-test/github-webhook-secret",
        "github_pat_secret_name": "treadmill-test/github-pat",
        "worker_aws_credentials_secret_name": (
            "treadmill-test/worker-aws-credentials"
        ),
        "api_aws_credentials_secret_name": (
            "treadmill-test/api-aws-credentials"
        ),
    }

    # local block — constants, not from CFN
    assert config["local"] == {
        "database_url": (
            "postgresql://treadmill:treadmill@localhost:5432/treadmill"
        ),
        "redis_url": "redis://localhost:6379/0",
        "api_url": "http://localhost:8088",
    }


def test_build_deployment_config_deployment_mode_is_dev_local_snake_case(
    synthetic_outputs,
):
    """ADR-0016 §"Canonical spellings": YAML field value is ``dev_local``."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )
    assert config["deployment_mode"] == "dev_local"


def test_build_deployment_config_coerces_account_id_to_str(synthetic_outputs):
    """Passing an int account_id is coerced to a str so the YAML round-trip
    preserves leading-zero accounts (defensive against accidental int)."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id=111111111111,  # type: ignore[arg-type]
        outputs=synthetic_outputs,
    )
    assert config["aws_account_id"] == "111111111111"
    assert isinstance(config["aws_account_id"], str)


def test_build_deployment_config_missing_output_raises_naming_the_suffix(
    synthetic_outputs,
):
    """If a required CFN output is missing, KeyError names which suffix."""
    incomplete = {
        k: v for k, v in synthetic_outputs.items()
        if "WebhookApiUrl" not in k
    }
    with pytest.raises(KeyError, match="WebhookApiUrl"):
        build_deployment_config(
            "test",
            aws_profile="treadmill-test",
            aws_region="us-east-1",
            aws_account_id="111111111111",
            outputs=incomplete,
        )


def test_build_deployment_config_missing_secret_output_raises(synthetic_outputs):
    """Missing secret-name output raises naming that specific suffix."""
    incomplete = {
        k: v for k, v in synthetic_outputs.items()
        if "GithubPatSecretName" not in k
    }
    with pytest.raises(KeyError, match="GithubPatSecretName"):
        build_deployment_config(
            "test",
            aws_profile="treadmill-test",
            aws_region="us-east-1",
            aws_account_id="111111111111",
            outputs=incomplete,
        )


def test_build_deployment_config_missing_deploy_events_queue_raises(synthetic_outputs):
    """Missing DeployEventsQueueUrl raises KeyError naming the suffix."""
    incomplete = {k: v for k, v in synthetic_outputs.items() if "DeployEventsQueueUrl" not in k}
    with pytest.raises(KeyError, match="DeployEventsQueueUrl"):
        build_deployment_config(
            "test",
            aws_profile="treadmill-test",
            aws_region="us-east-1",
            aws_account_id="111111111111",
            outputs=incomplete,
        )


def test_build_deployment_config_missing_deploy_events_dlq_raises(synthetic_outputs):
    """Missing DeployEventsDlqUrl raises KeyError naming the suffix."""
    incomplete = {k: v for k, v in synthetic_outputs.items() if "DeployEventsDlqUrl" not in k}
    with pytest.raises(KeyError, match="DeployEventsDlqUrl"):
        build_deployment_config(
            "test",
            aws_profile="treadmill-test",
            aws_region="us-east-1",
            aws_account_id="111111111111",
            outputs=incomplete,
        )


def test_build_deployment_config_stamps_dev_local_otel_collector_default(
    synthetic_outputs,
):
    """When ``TreadmillObservabilityStack`` was not deployed (the normal
    dev_local case), ``build_deployment_config`` stamps a container-DNS
    default for ``observability_collector_endpoint`` so worker + API
    containers on the ``treadmill-local`` docker network reach the
    sibling ``treadmill-otel-collector`` container by name (not via
    ``127.0.0.1``, which inside a container is the container itself)."""
    # synthetic_outputs deliberately has no ObservabilityCollectorEndpoint;
    # the absence is the normal dev_local case.
    assert not any(
        "ObservabilityCollectorEndpoint" in k for k in synthetic_outputs
    )
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )
    assert config["aws"]["observability_collector_endpoint"] == (
        "http://treadmill-otel-collector:4318"
    )


def test_build_deployment_config_cfn_observability_output_wins_over_default(
    synthetic_outputs,
):
    """When ``TreadmillObservabilityStack`` IS deployed (fully_remote
    crossover, or a dev_local operator who opted in), the CFN-provided
    endpoint wins over the dev-local container-DNS default."""
    outputs = dict(synthetic_outputs)
    outputs["ObservabilityCollectorEndpointDEADBEEF"] = "http://10.0.1.42:4318"
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=outputs,
    )
    assert config["aws"]["observability_collector_endpoint"] == (
        "http://10.0.1.42:4318"
    )


def test_build_deployment_config_stamps_grafana_host_and_port_defaults(
    synthetic_outputs,
):
    """When ``TreadmillObservabilityStack`` was not deployed (the normal
    dev_local case), ``build_deployment_config`` stamps the operator-
    facing Grafana defaults: 127.0.0.1 + port 3001. The port is 3001
    (not 3000) to sidestep the common laptop port-3000 collision
    (bunkhouse-dashboard observed 2026-05-19); ``treadmill-local up``
    binds Grafana to this port, and ``treadmill observe`` reads the
    same field so the URL it opens matches the binding."""
    assert not any(
        "ObservabilityGrafanaHost" in k for k in synthetic_outputs
    )
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )
    assert config["aws"]["observability_grafana_host"] == "127.0.0.1"
    assert config["aws"]["observability_grafana_port"] == 3001


def test_build_deployment_config_cfn_grafana_host_wins_over_default(
    synthetic_outputs,
):
    """When ``ObservabilityGrafanaHost`` IS provided (fully_remote
    deployments), the CFN-provided EC2 private IP wins over the
    dev-local 127.0.0.1 default. The port stays at the dev-local 3001
    default because CFN doesn't emit a port output — fully_remote
    Grafana also listens on 3000, but operators reach it via SSM-forwarded
    tunnels, not direct binding."""
    outputs = dict(synthetic_outputs)
    outputs["ObservabilityGrafanaHostDEADBEEF"] = "10.0.1.42"
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=outputs,
    )
    assert config["aws"]["observability_grafana_host"] == "10.0.1.42"


def test_build_deployment_config_matches_unhashed_output_keys(synthetic_outputs):
    """A user-renamed output (no CDK hash) still matches via direct endswith."""
    outputs = dict(synthetic_outputs)
    # Drop the hashed one + add a clean key for one of the contract names.
    del outputs["WebhookReceiverWebhookApiUrl51C59AB0"]
    outputs["WebhookApiUrl"] = "https://renamed.example.com"
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=outputs,
    )
    assert config["aws"]["webhook_api_url"] == "https://renamed.example.com"


# ── write_deployment_yaml ─────────────────────────────────────────────────────


def test_write_deployment_yaml_round_trip(synthetic_outputs, tmp_path: Path):
    """Write → read → assert structural equality (modulo YAML formatting)."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )
    target = tmp_path / "test.yaml"
    written = write_deployment_yaml("test", config, path=target)
    assert written == target
    assert target.exists()

    loaded = yaml.safe_load(target.read_text())
    assert loaded == config
    # Re-assert structural invariants on the loaded form (paranoia).
    assert loaded["deployment_id"] == "test"
    assert loaded["deployment_mode"] == "dev_local"
    assert loaded["aws_account_id"] == "111111111111"
    assert "events_topic_arn" in loaded["aws"]
    assert "github_pat_secret_name" in loaded["secrets"]
    assert "database_url" in loaded["local"]


def test_write_deployment_yaml_quotes_account_id_in_text(
    synthetic_outputs, tmp_path: Path,
):
    """Account ID is emitted as a quoted YAML string (Bash leading-zero
    defense). PyYAML quotes all-digit strings by default."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="012345678901",  # leading zero
        outputs=synthetic_outputs,
    )
    target = tmp_path / "test.yaml"
    write_deployment_yaml("test", config, path=target)
    text = target.read_text()
    # PyYAML quotes the leading-zero string because it isn't a valid bare
    # YAML scalar (would be parsed as octal otherwise).
    assert "aws_account_id: '012345678901'" in text or 'aws_account_id: "012345678901"' in text


def test_write_deployment_yaml_creates_parent_directory(
    synthetic_outputs, tmp_path: Path,
):
    """mkdir -p semantics: parent dir is created if absent."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )
    target = tmp_path / "nested" / "deeper" / "test.yaml"
    written = write_deployment_yaml("test", config, path=target)
    assert written.exists()
    assert written.parent.is_dir()


def test_write_deployment_yaml_overwrites_existing_file(
    synthetic_outputs, tmp_path: Path,
):
    """Idempotent re-run: existing file is overwritten."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )
    target = tmp_path / "test.yaml"
    target.write_text("stale: content\n")
    write_deployment_yaml("test", config, path=target)
    loaded = yaml.safe_load(target.read_text())
    assert "stale" not in loaded
    assert loaded["deployment_id"] == "test"


def test_write_deployment_yaml_top_level_key_order_matches_adr_0016(
    synthetic_outputs, tmp_path: Path,
):
    """The emitted file's top-level keys appear in ADR-0016's documented
    order. ``sort_keys=False`` in the dumper keeps insertion order."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )
    target = tmp_path / "test.yaml"
    write_deployment_yaml("test", config, path=target)
    lines = [
        ln.split(":", 1)[0]
        for ln in target.read_text().splitlines()
        if ln and not ln.startswith((" ", "#"))
    ]
    expected = [
        "deployment_id",
        "deployment_mode",
        "aws_profile",
        "aws_region",
        "aws_account_id",
        "aws",
        "secrets",
        "local",
        # ADR-0018: autoscaler block stamped at the end of the file by
        # ``treadmill-local init`` so operators see the defaults.
        "autoscaler",
    ]
    assert lines == expected


# ── load_deployment_yaml: backward-compatibility ──────────────────────────────


def test_load_deployment_yaml_missing_deploy_events_queue_url_raises_clear_error(
    synthetic_outputs, tmp_path: Path
):
    """Loading an older YAML without deploy_events_queue_url raises ValueError."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )
    del config["aws"]["deploy_events_queue_url"]
    target = tmp_path / "test.yaml"
    write_deployment_yaml("test", config, path=target)
    with pytest.raises(ValueError, match="deploy_events_queue_url"):
        load_deployment_yaml("test", path=target)


def test_load_deployment_yaml_missing_deploy_events_dlq_url_raises_clear_error(
    synthetic_outputs, tmp_path: Path
):
    """Loading an older YAML without deploy_events_dlq_url raises ValueError."""
    config = build_deployment_config(
        "test",
        aws_profile="treadmill-test",
        aws_region="us-east-1",
        aws_account_id="111111111111",
        outputs=synthetic_outputs,
    )
    del config["aws"]["deploy_events_dlq_url"]
    target = tmp_path / "test.yaml"
    write_deployment_yaml("test", config, path=target)
    with pytest.raises(ValueError, match="deploy_events_dlq_url"):
        load_deployment_yaml("test", path=target)


# ── End-to-end CLI: ``treadmill-local init <deployment_id>`` ──────────────────


@pytest.fixture
def patched_session(monkeypatch):
    """Replace ``boto3.Session`` in cli + deployment_config modules with a
    profile-ignoring variant. The CLI's ``--profile`` flag is required but
    no real AWS config file exists in test environments; the wrapper drops
    ``profile_name`` so ``boto3.Session(region_name=...)`` resolves
    against the env-var credential chain that moto intercepts.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    from treadmill_local import cli as cli_module
    from treadmill_local import deployment_config as dc_module

    _real_session = boto3.Session

    def _session_no_profile(profile_name=None, region_name=None):
        return _real_session(region_name=region_name or "us-east-1")

    monkeypatch.setattr(cli_module.boto3, "Session", _session_no_profile)
    monkeypatch.setattr(dc_module.boto3, "Session", _session_no_profile)


@mock_aws
def test_init_cli_end_to_end_writes_yaml(tmp_path: Path, patched_session):
    """Invoke the Typer CLI's ``init`` subcommand with mocked AWS.

    Sets up a real CFN stack with outputs in moto, then asserts the
    written YAML matches the ADR-0016 schema.
    """

    # Create a CFN stack with the outputs the init command expects.
    import json as _json
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    template = {
        "Resources": {
            "Dummy": {
                "Type": "AWS::SNS::Topic",
                "Properties": {"TopicName": "dummy"},
            }
        },
        "Outputs": {
            "MessagingEventsTopicArnA1B2C3D4": {
                "Value": "arn:aws:sns:us-east-1:111111111111:treadmill-test-events",
            },
            "MessagingEventsQueueUrlE5F6A7B8": {
                "Value": (
                    "https://sqs.us-east-1.amazonaws.com/111111111111/"
                    "treadmill-test-coordination"
                ),
            },
            "MessagingWorkQueueUrlC9D0E1F2": {
                "Value": (
                    "https://sqs.us-east-1.amazonaws.com/111111111111/"
                    "treadmill-test-work.fifo"
                ),
            },
            "WebhookReceiverWebhookApiUrl51C59AB0": {
                "Value": "https://abc123.execute-api.us-east-1.amazonaws.com",
            },
            "WebhookReceiverWebhookInboxQueueUrl1234ABCD": {
                "Value": (
                    "https://sqs.us-east-1.amazonaws.com/111111111111/"
                    "treadmill-test-webhook-inbox"
                ),
            },
            "WebhookReceiverWebhookInboxDlqUrlABCD1234": {
                "Value": (
                    "https://sqs.us-east-1.amazonaws.com/111111111111/"
                    "treadmill-test-webhook-inbox-dlq"
                ),
            },
            "SecretsGithubWebhookSecretName11112222": {
                "Value": "treadmill-test/github-webhook-secret",
            },
            "SecretsGithubPatSecretName33334444": {
                "Value": "treadmill-test/github-pat",
            },
            "SecretsWorkerAwsCredentialsSecretName55556666": {
                "Value": "treadmill-test/worker-aws-credentials",
            },
            "SecretsApiAwsCredentialsSecretName77778888": {
                "Value": "treadmill-test/api-aws-credentials",
            },
            "DeployEventsQueueUrl12345678": {
                "Value": (
                    "https://sqs.us-east-1.amazonaws.com/111111111111/"
                    "treadmill-test-deploy-events"
                ),
            },
            "DeployEventsDlqUrlABCDEFGH": {
                "Value": (
                    "https://sqs.us-east-1.amazonaws.com/111111111111/"
                    "treadmill-test-deploy-events-dlq"
                ),
            },
        },
    }
    cfn.create_stack(
        StackName="TreadmillTestCloudLite",
        TemplateBody=_json.dumps(template),
    )

    target = tmp_path / "test.yaml"
    result = runner.invoke(
        app,
        [
            "init", "test",
            "--profile", "treadmill-test",
            "--region", "us-east-1",
            "--output-path", str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert target.exists()

    loaded = yaml.safe_load(target.read_text())
    assert loaded["deployment_id"] == "test"
    assert loaded["deployment_mode"] == "dev_local"
    assert loaded["aws_profile"] == "treadmill-test"
    assert loaded["aws_region"] == "us-east-1"
    # moto's get_caller_identity returns 123456789012 by default; assert
    # the YAML field is a 12-char str (whatever it is).
    assert isinstance(loaded["aws_account_id"], str)
    assert len(loaded["aws_account_id"]) == 12
    assert loaded["aws"]["webhook_api_url"] == (
        "https://abc123.execute-api.us-east-1.amazonaws.com"
    )
    assert loaded["aws"]["events_topic_arn"] == (
        "arn:aws:sns:us-east-1:111111111111:treadmill-test-events"
    )
    assert loaded["secrets"]["github_pat_secret_name"] == (
        "treadmill-test/github-pat"
    )
    assert loaded["secrets"]["api_aws_credentials_secret_name"] == (
        "treadmill-test/api-aws-credentials"
    )
    assert loaded["local"]["database_url"].startswith("postgresql://")


@mock_aws
def test_init_cli_overwrites_existing_yaml(tmp_path: Path, patched_session):
    """Running init twice overwrites the YAML and prints a notice."""
    import json as _json
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    template = {
        "Resources": {
            "Dummy": {"Type": "AWS::SNS::Topic", "Properties": {"TopicName": "dummy"}},
        },
        "Outputs": {
            "MessagingEventsTopicArnAAAA1111": {"Value": "arn:aws:sns:..."},
            "MessagingEventsQueueUrlBBBB2222": {"Value": "queue1"},
            "MessagingWorkQueueUrlCCCC3333": {"Value": "queue2"},
            "WebhookReceiverWebhookApiUrlDDDD4444": {"Value": "url1"},
            "WebhookReceiverWebhookInboxQueueUrlEEEE5555": {"Value": "queue3"},
            "WebhookReceiverWebhookInboxDlqUrlFFFF6666": {"Value": "queue4"},
            "SecretsGithubWebhookSecretName11112222": {"Value": "secret1"},
            "SecretsGithubPatSecretName33334444": {"Value": "secret2"},
            "SecretsWorkerAwsCredentialsSecretName55556666": {"Value": "secret3"},
            "SecretsApiAwsCredentialsSecretName77778888": {"Value": "secret4"},
            "DeployEventsQueueUrlAAAA1111": {"Value": "https://sqs.us-east-1.amazonaws.com/111111111111/deploy-events"},
            "DeployEventsDlqUrlBBBB2222": {"Value": "https://sqs.us-east-1.amazonaws.com/111111111111/deploy-events-dlq"},
        },
    }
    cfn.create_stack(
        StackName="TreadmillTestCloudLite",
        TemplateBody=_json.dumps(template),
    )

    target = tmp_path / "test.yaml"
    target.write_text("stale: yes\n")  # pre-existing

    result = runner.invoke(
        app,
        [
            "init", "test",
            "--profile", "treadmill-test",
            "--output-path", str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Overwriting" in result.output
    loaded = yaml.safe_load(target.read_text())
    assert "stale" not in loaded
    assert loaded["deployment_id"] == "test"


@mock_aws
def test_init_cli_uses_default_stack_name(tmp_path: Path, patched_session):
    """``treadmill-local init personal`` defaults stack to
    ``TreadmillPersonalCloudLite``."""
    import json as _json
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    template = {
        "Resources": {
            "Dummy": {"Type": "AWS::SNS::Topic", "Properties": {"TopicName": "dummy"}},
        },
        "Outputs": {
            "MessagingEventsTopicArnX1": {"Value": "v1"},
            "MessagingEventsQueueUrlX2": {"Value": "v2"},
            "MessagingWorkQueueUrlX3": {"Value": "v3"},
            "WebhookReceiverWebhookApiUrlX4": {"Value": "v4"},
            "WebhookReceiverWebhookInboxQueueUrlX5": {"Value": "v5"},
            "WebhookReceiverWebhookInboxDlqUrlX6": {"Value": "v6"},
            "SecretsGithubWebhookSecretNameX7": {"Value": "v7"},
            "SecretsGithubPatSecretNameX8": {"Value": "v8"},
            "SecretsWorkerAwsCredentialsSecretNameX9": {"Value": "v9"},
            "SecretsApiAwsCredentialsSecretNameX10": {"Value": "v10"},
            "DeployEventsQueueUrlX11": {"Value": "https://sqs.us-east-1.amazonaws.com/111111111111/deploy-events"},
            "DeployEventsDlqUrlX12": {"Value": "https://sqs.us-east-1.amazonaws.com/111111111111/deploy-events-dlq"},
        },
    }
    # Stack name derived from deployment_id="personal".
    cfn.create_stack(
        StackName="TreadmillPersonalCloudLite",
        TemplateBody=_json.dumps(template),
    )

    target = tmp_path / "personal.yaml"
    result = runner.invoke(
        app,
        [
            "init", "personal",
            "--profile", "treadmill-personal",
            "--output-path", str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert target.exists()
    loaded = yaml.safe_load(target.read_text())
    assert loaded["deployment_id"] == "personal"


@mock_aws
def test_init_cli_missing_stack_exits_nonzero(tmp_path: Path, patched_session):
    """Missing CFN stack → exit code 1 with a clear message."""
    result = runner.invoke(
        app,
        [
            "init", "nonexistent",
            "--profile", "treadmill-nope",
            "--output-path", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 1
    assert "TreadmillNonexistentCloudLite" in result.output
