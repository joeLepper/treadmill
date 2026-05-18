"""Unit tests for the DeployWatcher dispatch logic, category actions, state-file
idempotency, and 404 error handling."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from treadmill_local.deploy_watcher import (
    DeployWatcher,
    _categorize_file,
    _categorize_files,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sqs_msg(pr_number: int, sha: str) -> dict:
    """Build a minimal SQS message with the SNS notification wrapper."""
    return {
        "ReceiptHandle": f"rh-{pr_number}",
        "Body": json.dumps({
            "Type": "Notification",
            "MessageId": "mid",
            "Message": json.dumps({
                "pr_number": pr_number,
                "merge_commit_sha": sha,
            }),
        }),
    }


def _make_watcher(
    tmp_path: Path,
    *,
    pr_files: list[str] | None = None,
    pr_files_fn=None,
) -> tuple[DeployWatcher, list[str]]:
    """Construct a watcher with controllable PR-files response.

    Tests call ``_process_message`` directly, so ``receive_fn`` is a no-op.
    Returns ``(watcher, acked_handles)``.
    """
    acked: list[str] = []

    if pr_files_fn is None:
        _files = pr_files if pr_files is not None else []

        def pr_files_fn(pr_number: int) -> list[str] | None:
            return _files

    watcher = DeployWatcher(
        receive_fn=lambda: [],
        ack_fn=lambda h: acked.append(h),
        get_pr_files_fn=pr_files_fn,
        state_file=tmp_path / "state.json",
        repo_root=Path("/fake-repo"),
    )
    return watcher, acked


# ── Categorization ────────────────────────────────────────────────────────────


def test_categorize_api():
    assert _categorize_file("services/api/main.py") == "api"


def test_categorize_agent():
    assert _categorize_file("workers/agent/src/runner.py") == "agent"


def test_categorize_infra():
    assert _categorize_file("infra/observability/dashboards/api.json") == "infra"


def test_categorize_adapter():
    assert _categorize_file("tools/local-adapter/treadmill_local/cli.py") == "adapter"


def test_categorize_ignored():
    assert _categorize_file("README.md") is None
    assert _categorize_file("docs/adrs/0001.md") is None
    assert _categorize_file("services/api-v2/main.py") is None  # not a prefix match


def test_categorize_files_groups_by_category():
    result = _categorize_files([
        "services/api/app.py",
        "workers/agent/run.py",
        "README.md",
    ])
    assert result == {
        "api": ["services/api/app.py"],
        "agent": ["workers/agent/run.py"],
    }


# ── Dispatch ordering ─────────────────────────────────────────────────────────


def test_dispatch_ordering_infra_not_api():
    """Files under infra/ must categorize as infra regardless of subdirectory name."""
    assert _categorize_file("infra/observability/dashboards/foo") == "infra"


def test_dispatch_ordering_infra_observability_dashboards():
    """Concrete case from the task spec: infra/observability/dashboards/ → infra."""
    result = _categorize_file("infra/observability/dashboards/api-latency.json")
    assert result == "infra"


# ── API category action ───────────────────────────────────────────────────────


@patch("subprocess.run")
def test_api_action_builds_and_restarts(mock_run, tmp_path):
    watcher, acked = _make_watcher(tmp_path, pr_files=["services/api/main.py"])

    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(1, "abc123"))

    assert mock_run.call_count == 2
    build_call, restart_call = mock_run.call_args_list
    assert build_call.args[0] == [
        "docker", "build", "-t", "treadmill-api:dev", "/fake-repo/services/api",
    ]
    assert restart_call.args[0] == ["docker", "restart", "treadmill-api"]
    assert acked == ["rh-1"]


# ── Agent category action ─────────────────────────────────────────────────────


@patch("subprocess.run")
def test_agent_action_builds_only(mock_run, tmp_path):
    """Agent build must NOT restart a container (workers are one-shot per ADR-0018)."""
    watcher, acked = _make_watcher(tmp_path, pr_files=["workers/agent/Dockerfile"])

    watcher._process_message(_sqs_msg(2, "def456"))

    assert mock_run.call_count == 1
    cmd = mock_run.call_args_list[0].args[0]
    assert "treadmill-agent:dev" in cmd
    assert "/fake-repo" in cmd
    assert "/fake-repo/workers/agent/Dockerfile" in cmd
    assert acked == ["rh-2"]


# ── Notify-only categories ────────────────────────────────────────────────────


@patch("subprocess.run")
def test_infra_notify_only(mock_run, tmp_path):
    """infra changes must log a notification and NOT call subprocess."""
    watcher, acked = _make_watcher(tmp_path, pr_files=["infra/main.tf"])

    watcher._process_message(_sqs_msg(3, "ghi789"))

    mock_run.assert_not_called()
    assert acked == ["rh-3"]


@patch("subprocess.run")
def test_adapter_notify_only(mock_run, tmp_path):
    """adapter changes must log a notification and NOT call subprocess."""
    watcher, acked = _make_watcher(tmp_path, pr_files=["tools/local-adapter/pyproject.toml"])

    watcher._process_message(_sqs_msg(4, "jkl012"))

    mock_run.assert_not_called()
    assert acked == ["rh-4"]


# ── State-file idempotency ────────────────────────────────────────────────────


@patch("subprocess.run")
def test_idempotency_skips_rebuild_for_same_sha(mock_run, tmp_path):
    """Re-delivered event with the same SHA + category must not trigger a rebuild."""
    watcher, acked = _make_watcher(tmp_path, pr_files=["services/api/app.py"])
    sha = "abc123deadbeef"

    # First delivery: action runs.
    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(5, sha))
    assert mock_run.call_count == 2  # build + restart

    # Second delivery of the same event: no action, still acked.
    mock_run.reset_mock()
    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(5, sha))

    mock_run.assert_not_called()
    assert len(acked) == 2  # both deliveries were acked


@patch("subprocess.run")
def test_idempotency_rebuilds_for_new_sha(mock_run, tmp_path):
    """A different SHA for the same category must trigger a fresh build."""
    watcher, acked = _make_watcher(tmp_path, pr_files=["services/api/app.py"])

    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(6, "sha-one"))
    assert mock_run.call_count == 2

    mock_run.reset_mock()
    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(6, "sha-two"))
    assert mock_run.call_count == 2  # rebuild triggered


@patch("subprocess.run")
def test_state_file_written_after_success(mock_run, tmp_path):
    """State file must record the SHA for each applied category."""
    watcher, _ = _make_watcher(tmp_path, pr_files=["workers/agent/run.py"])
    sha = "statecheck"

    watcher._process_message(_sqs_msg(7, sha))

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["agent"] == sha


# ── gh API 404 handling ───────────────────────────────────────────────────────


@patch("subprocess.run")
def test_pr_not_found_acks_and_skips(mock_run, tmp_path):
    """When the gh API returns 404 (PR deleted), ack the message without any action."""

    def pr_files_fn(pr_number: int) -> list[str] | None:
        return None  # simulates 404

    watcher, acked = _make_watcher(tmp_path, pr_files_fn=pr_files_fn)

    watcher._process_message(_sqs_msg(99, "orphan-sha"))

    mock_run.assert_not_called()
    assert acked == ["rh-99"]
