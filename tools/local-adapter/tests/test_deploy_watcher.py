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
    """Build a minimal SQS message with the SNS notification wrapper.

    The inner ``Message`` matches the record shape produced by the API's
    ``eventbus._build_record`` for a ``github.pr_merged`` event: top-level
    metadata (entity_type/action/...) plus a nested ``payload`` carrying
    the typed ``GithubPrMerged`` fields (``pr_number``, ``merged_sha``).
    Earlier versions of this helper put the typed fields at the top level,
    which masked an envelope bug in the watcher (KeyError 'pr_number' on
    real SNS messages) — keep this contract aligned with eventbus.py.
    """
    return {
        "ReceiptHandle": f"rh-{pr_number}",
        "Body": json.dumps({
            "Type": "Notification",
            "MessageId": "mid",
            "Message": json.dumps({
                "event_id": f"evt-{pr_number}",
                "entity_type": "github",
                "action": "pr_merged",
                "task_id": None,
                "plan_id": None,
                "run_id": None,
                "step_id": None,
                "payload": {
                    "repo": "joeLepper/treadmill",
                    "pr_number": pr_number,
                    "sender": "tester",
                    "merged_sha": sha,
                    "head_branch": f"feat-{pr_number}",
                },
            }),
        }),
    }


def _sqs_msg_malformed(receipt: str, inner_message: dict) -> dict:
    """Build a minimal SQS message with an explicitly-shaped inner Message.

    Lets tests force missing/extra fields to verify the watcher acks-and-skips
    rather than re-receiving forever on a schema-drift event.
    """
    return {
        "ReceiptHandle": receipt,
        "Body": json.dumps({
            "Type": "Notification",
            "MessageId": "mid",
            "Message": json.dumps(inner_message),
        }),
    }


def _make_watcher(
    tmp_path: Path,
    *,
    pr_files: list[str] | None = None,
    pr_files_fn=None,
    api_health_url: str = "http://localhost:8088/health/ready",
) -> tuple[DeployWatcher, list[str], list[str]]:
    """Construct a watcher with controllable PR-files response.

    Tests call ``_process_message`` directly, so ``receive_fn`` is a no-op.
    The ``recreate_api_fn`` is a recording stub — every test that exercises
    the api category needs to verify it WAS called (the silent-no-op
    ADR-0024 captured was the watcher skipping recreate entirely).

    Returns ``(watcher, acked_handles, recreate_calls)``.
    """
    acked: list[str] = []
    recreate_calls: list[str] = []

    if pr_files_fn is None:
        _files = pr_files if pr_files is not None else []

        def pr_files_fn(pr_number: int) -> list[str] | None:
            return _files

    def recreate_api() -> None:
        recreate_calls.append("recreate")

    watcher = DeployWatcher(
        receive_fn=lambda: [],
        ack_fn=lambda h: acked.append(h),
        get_pr_files_fn=pr_files_fn,
        recreate_api_fn=recreate_api,
        api_health_url=api_health_url,
        state_file=tmp_path / "state.json",
        repo_root=Path("/fake-repo"),
    )
    return watcher, acked, recreate_calls


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
def test_api_action_builds_and_recreates(mock_run, tmp_path):
    """The api action must build the image AND call the runtime recreate
    helper — NOT ``docker restart``, which re-runs the EXISTING container's
    image (the silent-no-op ADR-0024 captured)."""
    watcher, acked, recreate_calls = _make_watcher(
        tmp_path, pr_files=["services/api/main.py"],
    )

    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(1, "abc123"))

    # Exactly one subprocess call — the docker build. The recreate path
    # goes through the injected runtime helper, NOT a ``docker restart``
    # subprocess shell-out.
    assert mock_run.call_count == 1
    build_call = mock_run.call_args_list[0]
    assert build_call.args[0] == [
        "docker", "build", "-t", "treadmill-api:dev", "/fake-repo/services/api",
    ]
    # No ``docker restart`` anywhere — guard against the regression.
    for call in mock_run.call_args_list:
        cmd = call.args[0]
        assert "restart" not in cmd, (
            f"deploy-watcher must not docker-restart the API; got cmd={cmd}"
        )
    # The recreate helper was invoked exactly once after the build —
    # this is what actually swaps the running container to the new image.
    assert recreate_calls == ["recreate"]
    assert acked == ["rh-1"]


@patch("subprocess.run")
def test_api_action_health_url_uses_configured_port(mock_run, tmp_path):
    """``_wait_healthy`` must be called with the port from the deployment
    config (the watcher composes the URL from ``cfg['local']['api_url']``),
    NOT the hardcoded ``:8000`` the original watcher used (the dev-local
    API serves on ``:8088``, so the old probe always timed out against
    the wrong port and falsely reported the deploy as unhealthy)."""
    configured_url = "http://localhost:8088/health/ready"
    watcher, _, _ = _make_watcher(
        tmp_path,
        pr_files=["services/api/main.py"],
        api_health_url=configured_url,
    )

    with patch.object(watcher, "_wait_healthy") as wait_mock:
        watcher._process_message(_sqs_msg(11, "abc123"))

    wait_mock.assert_called_once()
    called_url = wait_mock.call_args.args[0]
    assert called_url == configured_url
    assert ":8000" not in called_url, (
        "deploy-watcher must not health-check the hardcoded :8000 port"
    )


# ── Agent category action ─────────────────────────────────────────────────────


@patch("subprocess.run")
def test_agent_action_builds_only(mock_run, tmp_path):
    """Agent build must NOT restart a container (workers are one-shot per ADR-0018)."""
    watcher, acked, _ = _make_watcher(
        tmp_path, pr_files=["workers/agent/Dockerfile"],
    )

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
    watcher, acked, _ = _make_watcher(tmp_path, pr_files=["infra/main.tf"])

    watcher._process_message(_sqs_msg(3, "ghi789"))

    mock_run.assert_not_called()
    assert acked == ["rh-3"]


@patch("subprocess.run")
def test_adapter_notify_only(mock_run, tmp_path):
    """adapter changes must log a notification and NOT call subprocess."""
    watcher, acked, _ = _make_watcher(
        tmp_path, pr_files=["tools/local-adapter/pyproject.toml"],
    )

    watcher._process_message(_sqs_msg(4, "jkl012"))

    mock_run.assert_not_called()
    assert acked == ["rh-4"]


# ── State-file idempotency ────────────────────────────────────────────────────


@patch("subprocess.run")
def test_idempotency_skips_rebuild_for_same_sha(mock_run, tmp_path):
    """Re-delivered event with the same SHA + category must not trigger a rebuild."""
    watcher, acked, recreate_calls = _make_watcher(
        tmp_path, pr_files=["services/api/app.py"],
    )
    sha = "abc123deadbeef"

    # First delivery: action runs (1 subprocess call: docker build + 1 recreate).
    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(5, sha))
    assert mock_run.call_count == 1  # docker build only — recreate is injected
    assert recreate_calls == ["recreate"]

    # Second delivery of the same event: no action, still acked.
    mock_run.reset_mock()
    recreate_calls.clear()
    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(5, sha))

    mock_run.assert_not_called()
    assert recreate_calls == []
    assert len(acked) == 2  # both deliveries were acked


@patch("subprocess.run")
def test_idempotency_rebuilds_for_new_sha(mock_run, tmp_path):
    """A different SHA for the same category must trigger a fresh build."""
    watcher, acked, recreate_calls = _make_watcher(
        tmp_path, pr_files=["services/api/app.py"],
    )

    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(6, "sha-one"))
    assert mock_run.call_count == 1
    assert recreate_calls == ["recreate"]

    mock_run.reset_mock()
    recreate_calls.clear()
    with patch.object(watcher, "_wait_healthy"):
        watcher._process_message(_sqs_msg(6, "sha-two"))
    assert mock_run.call_count == 1  # rebuild triggered
    assert recreate_calls == ["recreate"]


@patch("subprocess.run")
def test_state_file_written_after_success(mock_run, tmp_path):
    """State file must record the SHA for each applied category."""
    watcher, _, _ = _make_watcher(tmp_path, pr_files=["workers/agent/run.py"])
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

    watcher, acked, _ = _make_watcher(tmp_path, pr_files_fn=pr_files_fn)

    watcher._process_message(_sqs_msg(99, "orphan-sha"))

    mock_run.assert_not_called()
    assert acked == ["rh-99"]


# ── Malformed-message handling (regression for KeyError stall) ───────────────


@patch("subprocess.run")
def test_missing_pr_number_acks_and_skips(mock_run, tmp_path):
    """A pr_merged record that's missing ``pr_number`` should be logged + acked,
    not stuck on the queue. Earlier code raised KeyError out of the poll loop,
    which left the message visible and Treadmill's pollers re-receiving it
    every 30s indefinitely (until maxReceiveCount=3 → DLQ)."""
    watcher, acked, _ = _make_watcher(tmp_path)
    msg = _sqs_msg_malformed(
        "rh-missing-pr",
        {
            "event_id": "evt-x",
            "entity_type": "github",
            "action": "pr_merged",
            "payload": {"repo": "x/y", "sender": "z", "merged_sha": "abc"},
        },
    )

    watcher._process_message(msg)

    mock_run.assert_not_called()
    assert acked == ["rh-missing-pr"]


@patch("subprocess.run")
def test_missing_payload_block_acks_and_skips(mock_run, tmp_path):
    """A record with no ``payload`` block at all (schema drift / truncated
    publish) should also ack-and-skip rather than wedge the watcher."""
    watcher, acked, _ = _make_watcher(tmp_path)
    msg = _sqs_msg_malformed(
        "rh-no-payload",
        {"event_id": "evt-x", "entity_type": "github", "action": "pr_merged"},
    )

    watcher._process_message(msg)

    mock_run.assert_not_called()
    assert acked == ["rh-no-payload"]
