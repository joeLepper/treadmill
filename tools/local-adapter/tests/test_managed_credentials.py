"""Tests for treadmill_local.managed_credentials (ADR-0072)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from treadmill_local import runtime as runtime_module
from treadmill_local.managed_credentials import (
    ManagedCredentialsFileError,
    resolve_boto3_session,
    resolve_managed_host_credentials,
)
from treadmill_local.runtime import LocalRuntime


# ── resolver unit tests ────────────────────────────────────────────────────────


def test_resolve_absent_file_returns_none(tmp_path: Path) -> None:
    """No credentials file → resolver returns None (caller uses SSO)."""
    result = resolve_managed_host_credentials(tmp_path / "missing.json")
    assert result is None


def test_resolve_well_formed_file_returns_env_dict(tmp_path: Path) -> None:
    """Valid JSON with both required keys + session_token → full env dict."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "session_token": "AQoDYXdzEJr...",
                "expires_at": "2026-12-31T23:59:59Z",
            }
        )
    )

    result = resolve_managed_host_credentials(creds_file)

    assert result == {
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "AWS_SESSION_TOKEN": "AQoDYXdzEJr...",
    }


def test_resolve_without_session_token(tmp_path: Path) -> None:
    """Valid JSON without session_token → dict has no AWS_SESSION_TOKEN."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            }
        )
    )

    result = resolve_managed_host_credentials(creds_file)

    assert result is not None
    assert "AWS_SESSION_TOKEN" not in result
    assert result["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
    assert result["AWS_SECRET_ACCESS_KEY"] == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_resolve_required_key_missing_raises(tmp_path: Path) -> None:
    """File with only access_key_id (no secret_access_key) → loud failure."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps({"access_key_id": "AKIAIOSFODNN7EXAMPLE"}))

    with pytest.raises(ManagedCredentialsFileError, match="secret_access_key"):
        resolve_managed_host_credentials(creds_file)


def test_resolve_access_key_id_missing_raises(tmp_path: Path) -> None:
    """File with only secret_access_key (no access_key_id) → loud failure."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(
        json.dumps({"secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"})
    )

    with pytest.raises(ManagedCredentialsFileError, match="access_key_id"):
        resolve_managed_host_credentials(creds_file)


def test_resolve_malformed_json_raises(tmp_path: Path) -> None:
    """File containing invalid JSON → raises ManagedCredentialsFileError."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text("this is not json {{{")

    with pytest.raises(ManagedCredentialsFileError):
        resolve_managed_host_credentials(creds_file)


def test_resolve_unreadable_file_raises(tmp_path: Path) -> None:
    """File present but not readable (mode 000) → raises ManagedCredentialsFileError."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps({"access_key_id": "x", "secret_access_key": "y"}))
    creds_file.chmod(0o000)

    try:
        with pytest.raises(ManagedCredentialsFileError):
            resolve_managed_host_credentials(creds_file)
    finally:
        # Restore permissions so tmp_path cleanup works.
        creds_file.chmod(0o644)


# ── resolve_boto3_session tests ───────────────────────────────────────────────


def test_resolve_boto3_session_file_absent_uses_profile(tmp_path: Path) -> None:
    with patch("treadmill_local.managed_credentials.boto3.Session") as mock_cls:
        resolve_boto3_session("my-profile", "us-east-1", tmp_path / "missing.json")
        mock_cls.assert_called_once_with(profile_name="my-profile", region_name="us-east-1")


def test_resolve_boto3_session_file_present_uses_keys(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps({
        "access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    }))
    with patch("treadmill_local.managed_credentials.boto3.Session") as mock_cls:
        resolve_boto3_session("my-profile", "us-east-1", creds_file)
        mock_cls.assert_called_once_with(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            region_name="us-east-1",
        )


def test_resolve_boto3_session_file_with_session_token(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps({
        "access_key_id": "AKIA",
        "secret_access_key": "SECRET",
        "session_token": "TOKEN",
    }))
    with patch("treadmill_local.managed_credentials.boto3.Session") as mock_cls:
        resolve_boto3_session("my-profile", "us-west-2", creds_file)
        mock_cls.assert_called_once_with(
            aws_access_key_id="AKIA",
            aws_secret_access_key="SECRET",
            aws_session_token="TOKEN",
            region_name="us-west-2",
        )


def test_resolve_boto3_session_malformed_raises(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    creds_file.write_text("not-json")
    with pytest.raises(ManagedCredentialsFileError):
        resolve_boto3_session("my-profile", "us-east-1", creds_file)


# ── runtime integration tests ─────────────────────────────────────────────────


def _valid_yaml_dict(deployment_id: str = "personal") -> dict[str, Any]:
    return {
        "deployment_id": deployment_id,
        "deployment_mode": "dev_local",
        "aws_profile": f"treadmill-{deployment_id}",
        "aws_region": "us-east-1",
        "aws_account_id": "111111111111",
        "aws": {
            "events_topic_arn": (
                f"arn:aws:sns:us-east-1:111111111111:treadmill-{deployment_id}-events"
            ),
            "events_queue_url": (
                f"https://sqs.us-east-1.amazonaws.com/111111111111/"
                f"treadmill-{deployment_id}-coordination"
            ),
            "work_queue_url": (
                f"https://sqs.us-east-1.amazonaws.com/111111111111/"
                f"treadmill-{deployment_id}-work.fifo"
            ),
            "webhook_inbox_queue_url": (
                f"https://sqs.us-east-1.amazonaws.com/111111111111/"
                f"treadmill-{deployment_id}-webhook-inbox"
            ),
            "webhook_inbox_dlq_url": (
                f"https://sqs.us-east-1.amazonaws.com/111111111111/"
                f"treadmill-{deployment_id}-webhook-inbox-dlq"
            ),
            "webhook_api_url": "https://abc123.execute-api.us-east-1.amazonaws.com",
            "deploy_events_queue_url": (
                f"https://sqs.us-east-1.amazonaws.com/111111111111/"
                f"treadmill-{deployment_id}-deploy-events"
            ),
            "deploy_events_dlq_url": (
                f"https://sqs.us-east-1.amazonaws.com/111111111111/"
                f"treadmill-{deployment_id}-deploy-events-dlq"
            ),
        },
        "secrets": {
            "github_webhook_secret_name": f"treadmill-{deployment_id}/github-webhook-secret",
            "github_pat_secret_name": f"treadmill-{deployment_id}/github-pat",
            "worker_aws_credentials_secret_name": (
                f"treadmill-{deployment_id}/worker-aws-credentials"
            ),
            "api_aws_credentials_secret_name": (
                f"treadmill-{deployment_id}/api-aws-credentials"
            ),
        },
        "local": {
            "database_url": "postgresql://treadmill:treadmill@localhost:5432/treadmill",
            "redis_url": "redis://localhost:6379/0",
            "api_url": "http://localhost:8088",
        },
        "autoscaler": {
            "min": 0,
            "max": 1,
            "tick_seconds": 5,
        },
    }


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock(name="fake_docker")
    monkeypatch.setattr(runtime_module.docker, "from_env", lambda: fake)
    return fake


def _make_fake_popen() -> tuple[list[dict[str, Any]], Any]:
    """Return (calls_list, fake_Popen_callable)."""
    calls: list[dict[str, Any]] = []

    class _FakeProc:
        pid = 9999

    def _popen(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append({"args": args, "kwargs": kwargs})
        return _FakeProc()

    return calls, _popen


def test_runtime_drops_aws_profile_when_managed_creds_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When managed credentials are present, AWS_PROFILE is absent from the
    subprocess env and the IAM key vars are set (ADR-0072)."""
    cfg = _valid_yaml_dict()
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWS_PROFILE", "treadmill-from-env")

    fake_managed = {
        "AWS_ACCESS_KEY_ID": "AKIAMANAGED",
        "AWS_SECRET_ACCESS_KEY": "managed-secret",
        "AWS_SESSION_TOKEN": "managed-token",
    }

    calls, fake_popen = _make_fake_popen()
    monkeypatch.setattr(runtime_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        runtime_module,
        "resolve_managed_host_credentials",
        lambda: fake_managed,
    )

    rt._start_autoscaler_dev_local()

    assert len(calls) == 1
    env = calls[0]["kwargs"]["env"]
    assert "AWS_PROFILE" not in env
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAMANAGED"
    assert env["AWS_SECRET_ACCESS_KEY"] == "managed-secret"
    assert env["AWS_SESSION_TOKEN"] == "managed-token"


def test_runtime_preserves_aws_profile_when_managed_creds_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When managed credentials file is absent (resolver returns None),
    AWS_PROFILE stays in the subprocess env and IAM key vars are absent."""
    cfg = _valid_yaml_dict()
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWS_PROFILE", "treadmill-from-env")

    calls, fake_popen = _make_fake_popen()
    monkeypatch.setattr(runtime_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        runtime_module,
        "resolve_managed_host_credentials",
        lambda: None,
    )

    rt._start_autoscaler_dev_local()

    assert len(calls) == 1
    env = calls[0]["kwargs"]["env"]
    assert env["AWS_PROFILE"] == "treadmill-from-env"
    assert "AWS_ACCESS_KEY_ID" not in env or env.get("AWS_ACCESS_KEY_ID") != "AKIAMANAGED"
    assert "AWS_SESSION_TOKEN" not in env
