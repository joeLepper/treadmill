"""Tests for ``treadmill_cli.observe`` — observe command group.

Covers:
- Config loading: missing file, missing grafana host, happy path.
- Reachability check: direct vs unreachable.
- URL construction: loki, tempo, prometheus, dashboard.
- CLI argument validation: missing --task / --metric / bad target.
- ``obs_open`` subcommand outputs correct URL to stdout.
- ``obs_status`` reports access method without opening browser.
"""

from __future__ import annotations

import json
import socket
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from treadmill_cli.observe import (
    _GRAFANA_PORT,
    _LOKI_UID,
    _PROMETHEUS_UID,
    _TEMPO_UID,
    build_explore_url,
    check_direct_reachable,
    dashboard_url,
    loki_url,
    observe_app,
    prometheus_url,
    tempo_url,
)


runner = CliRunner()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def deployment_yaml(tmp_path: Path) -> dict[str, Any]:
    """A minimal deployment YAML with observability fields populated."""
    config = {
        "deployment_id": "test",
        "deployment_mode": "dev_local",
        "aws_profile": "treadmill-test",
        "aws_region": "us-east-1",
        "aws_account_id": "111111111111",
        "aws": {
            "events_topic_arn": "arn:aws:sns:us-east-1:111111111111:events",
            "events_queue_url": "https://sqs.us-east-1.amazonaws.com/111111111111/events",
            "work_queue_url": "https://sqs.us-east-1.amazonaws.com/111111111111/work.fifo",
            "webhook_inbox_queue_url": "https://sqs.us-east-1.amazonaws.com/111111111111/inbox",
            "webhook_inbox_dlq_url": "https://sqs.us-east-1.amazonaws.com/111111111111/dlq",
            "webhook_api_url": "https://abc.execute-api.us-east-1.amazonaws.com",
            "observability_grafana_host": "10.0.1.42",
            "observability_ec2_id": "i-0abc1234def56789",
            "observability_collector_endpoint": "http://10.0.1.42:4318",
        },
        "secrets": {
            "github_webhook_secret_name": "s/webhook",
            "github_pat_secret_name": "s/pat",
            "worker_aws_credentials_secret_name": "s/worker",
            "api_aws_credentials_secret_name": "s/api",
        },
        "local": {
            "database_url": "postgresql://treadmill:treadmill@localhost:5432/treadmill",
            "redis_url": "redis://localhost:6379/0",
            "api_url": "http://localhost:8000",
        },
    }
    path = tmp_path / "test.yaml"
    path.write_text(yaml.dump(config))
    return {"config": config, "path": path}


@pytest.fixture
def patched_home(tmp_path: Path, deployment_yaml: dict[str, Any], monkeypatch):
    """Redirect Path.home() to tmp_path and write the deployment YAML there."""
    treadmill_dir = tmp_path / ".treadmill"
    treadmill_dir.mkdir()
    dest = treadmill_dir / "test.yaml"
    dest.write_text(yaml.dump(deployment_yaml["config"]))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return deployment_yaml["config"]


# ── URL construction ──────────────────────────────────────────────────────────


def test_loki_url_contains_task_id():
    url = loki_url("http://localhost:3000", "abc-123")
    assert "abc-123" in urllib.parse.unquote(url)
    assert "/explore" in url
    assert "orgId=1" in url
    left = json.loads(urllib.parse.unquote(url.split("left=", 1)[1]))
    assert left["datasource"] == _LOKI_UID
    assert "abc-123" in left["queries"][0]["expr"]


def test_tempo_url_contains_task_id():
    url = tempo_url("http://localhost:3000", "abc-123")
    left = json.loads(urllib.parse.unquote(url.split("left=", 1)[1]))
    assert left["datasource"] == _TEMPO_UID
    assert "abc-123" in left["queries"][0]["search"]


def test_prometheus_url_contains_metric():
    url = prometheus_url("http://localhost:3000", "worker_runs_total")
    left = json.loads(urllib.parse.unquote(url.split("left=", 1)[1]))
    assert left["datasource"] == _PROMETHEUS_UID
    assert left["queries"][0]["expr"] == "worker_runs_total"


def test_dashboard_url_shape():
    url = dashboard_url("http://localhost:3000", "treadmill-overview")
    assert url == "http://localhost:3000/d/treadmill-overview"


def test_build_explore_url_range_defaults():
    url = build_explore_url("http://localhost:3000", "loki", "expr", "{job='test'}")
    left = json.loads(urllib.parse.unquote(url.split("left=", 1)[1]))
    assert left["range"] == {"from": "now-1h", "to": "now"}


# ── Reachability ──────────────────────────────────────────────────────────────


def test_check_direct_reachable_returns_false_on_connection_refused():
    # Port 1 is almost certainly not listening; should fail quickly.
    result = check_direct_reachable("127.0.0.1", 1)
    assert result is False


def test_check_direct_reachable_returns_true_when_port_open(tmp_path):
    """Spin up a minimal TCP listener and confirm check_direct_reachable sees it."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)
    try:
        result = check_direct_reachable("127.0.0.1", port)
    finally:
        server.close()
    assert result is True


# ── Config loading ────────────────────────────────────────────────────────────


def test_load_obs_config_missing_file_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    result = runner.invoke(observe_app, [
        "status", "--deployment", "nonexistent",
    ])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_load_obs_config_missing_grafana_host_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    treadmill_dir = tmp_path / ".treadmill"
    treadmill_dir.mkdir()
    config = {"deployment_id": "test", "aws": {"webhook_api_url": "x"}}
    (treadmill_dir / "test.yaml").write_text(yaml.dump(config))

    result = runner.invoke(observe_app, ["status", "--deployment", "test"])
    assert result.exit_code == 2
    assert "observability_grafana_host" in result.output


# ── obs_open ──────────────────────────────────────────────────────────────────


def test_obs_open_dashboard_prints_url(patched_home):
    result = runner.invoke(observe_app, [
        "open", "dashboard", "--deployment", "test",
    ])
    assert result.exit_code == 0
    assert "/d/treadmill-overview" in result.output


def test_obs_open_logs_prints_loki_url(patched_home):
    result = runner.invoke(observe_app, [
        "open", "logs", "--deployment", "test", "--task", "abc-123",
    ])
    assert result.exit_code == 0
    assert "abc-123" in result.output
    assert "/explore" in result.output


def test_obs_open_traces_prints_tempo_url(patched_home):
    result = runner.invoke(observe_app, [
        "open", "traces", "--deployment", "test", "--task", "abc-123",
    ])
    assert result.exit_code == 0
    assert "abc-123" in result.output
    assert "/explore" in result.output


def test_obs_open_metrics_prints_prometheus_url(patched_home):
    result = runner.invoke(observe_app, [
        "open", "metrics", "--deployment", "test", "--metric", "worker_runs_total",
    ])
    assert result.exit_code == 0
    assert "worker_runs_total" in result.output


def test_obs_open_bad_target_exits(patched_home):
    result = runner.invoke(observe_app, [
        "open", "bogus", "--deployment", "test",
    ])
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_obs_open_logs_missing_task_exits(patched_home):
    result = runner.invoke(observe_app, [
        "open", "logs", "--deployment", "test",
    ])
    assert result.exit_code == 2
    assert "--task" in result.output


def test_obs_open_metrics_missing_metric_exits(patched_home):
    result = runner.invoke(observe_app, [
        "open", "metrics", "--deployment", "test",
    ])
    assert result.exit_code == 2
    assert "--metric" in result.output


# ── obs_status ────────────────────────────────────────────────────────────────


def test_obs_status_direct_reachable(patched_home):
    with patch(
        "treadmill_cli.observe.check_direct_reachable", return_value=True,
    ):
        result = runner.invoke(observe_app, ["status", "--deployment", "test"])
    assert result.exit_code == 0
    assert "reachable" in result.output
    assert "direct" in result.output


def test_obs_status_ssm_fallback(patched_home):
    with patch(
        "treadmill_cli.observe.check_direct_reachable", return_value=False,
    ):
        result = runner.invoke(observe_app, ["status", "--deployment", "test"])
    assert result.exit_code == 0
    assert "SSM" in result.output
    assert "i-0abc1234def56789" in result.output


def test_obs_status_no_ec2_id_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    treadmill_dir = tmp_path / ".treadmill"
    treadmill_dir.mkdir()
    config = {
        "deployment_id": "test",
        "aws": {
            "observability_grafana_host": "10.0.1.42",
            # no observability_ec2_id
        },
    }
    (treadmill_dir / "test.yaml").write_text(yaml.dump(config))

    with patch(
        "treadmill_cli.observe.check_direct_reachable", return_value=False,
    ):
        result = runner.invoke(observe_app, ["status", "--deployment", "test"])
    assert result.exit_code == 2


# ── obs_dashboard / obs_logs / obs_traces / obs_metrics (browser path) ────────


def _invoke_with_direct_reach(cmd: list[str]) -> Any:
    """Run cmd with direct reachability mocked True and webbrowser.open mocked."""
    with (
        patch("treadmill_cli.observe.check_direct_reachable", return_value=True),
        patch("webbrowser.open", return_value=True) as mock_browser,
    ):
        result = runner.invoke(observe_app, cmd)
    return result, mock_browser


def test_obs_dashboard_opens_browser(patched_home):
    result, mock_browser = _invoke_with_direct_reach([
        "dashboard", "--deployment", "test",
    ])
    assert result.exit_code == 0
    mock_browser.assert_called_once()
    url = mock_browser.call_args[0][0]
    assert "/d/treadmill-overview" in url


def test_obs_dashboard_custom_name(patched_home):
    result, mock_browser = _invoke_with_direct_reach([
        "dashboard", "--deployment", "test", "--name", "treadmill-claude-code",
    ])
    assert result.exit_code == 0
    url = mock_browser.call_args[0][0]
    assert "/d/treadmill-claude-code" in url


def test_obs_logs_opens_loki_explore(patched_home):
    result, mock_browser = _invoke_with_direct_reach([
        "logs", "--deployment", "test", "--task", "task-abc",
    ])
    assert result.exit_code == 0
    url = mock_browser.call_args[0][0]
    assert "task-abc" in urllib.parse.unquote(url)


def test_obs_traces_opens_tempo_explore(patched_home):
    result, mock_browser = _invoke_with_direct_reach([
        "traces", "--deployment", "test", "--task", "task-xyz",
    ])
    assert result.exit_code == 0
    url = mock_browser.call_args[0][0]
    assert "task-xyz" in urllib.parse.unquote(url)


def test_obs_metrics_opens_prometheus_explore(patched_home):
    result, mock_browser = _invoke_with_direct_reach([
        "metrics", "--deployment", "test", "--metric", "worker_run_duration_seconds",
    ])
    assert result.exit_code == 0
    url = mock_browser.call_args[0][0]
    assert "worker_run_duration_seconds" in urllib.parse.unquote(url)
