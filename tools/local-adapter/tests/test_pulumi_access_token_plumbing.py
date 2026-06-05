"""PULUMI_ACCESS_TOKEN worker-env plumbing.

Repos that need ``pulumi`` (Plan A GCP substrate, future GCP work) require
the CLI to authenticate non-interactively against Pulumi Cloud. The
``PULUMI_ACCESS_TOKEN`` env var is pulumi's non-interactive auth path; the
local-adapter's ``_dev_local_worker_env`` propagates it from the
autoscaler's environment into the worker container. Absent: env-var
omitted entirely so pulumi surfaces its own clean "PULUMI_ACCESS_TOKEN
must be set" message rather than the worker silently picking up the wrong
identity.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from treadmill_local.runtime import LocalRuntime


def _runtime_with_cfg(cfg: dict) -> LocalRuntime:
    rt = LocalRuntime(
        infra_dir=Path("/fake/infra"),
        deployment_config=cfg,
    )
    rt._worker_aws_env = {
        "AWS_ACCESS_KEY_ID": "TEST",
        "AWS_SECRET_ACCESS_KEY": "TEST",
    }
    return rt


def _base_cfg() -> dict:
    return {
        "deployment_id": "test",
        "aws_region": "us-west-2",
        "aws": {
            "events_topic_arn": "arn:test",
            "events_queue_url": "https://test/events",
            "work_queue_url": "https://test/work",
            "webhook_inbox_queue_url": "https://test/inbox",
        },
        "secrets": {
            "github_pat_secret_name": "test/pat",
        },
    }


def test_worker_env_carries_pulumi_token_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``PULUMI_ACCESS_TOKEN`` is in the autoscaler's env, the worker
    env carries it verbatim so pulumi CLI authenticates non-interactively."""
    monkeypatch.setenv("PULUMI_ACCESS_TOKEN", "pul-test-token-abc123")
    cfg = _base_cfg()
    rt = _runtime_with_cfg(cfg)
    with patch.object(rt, "_ensure_dev_local_credentials"):
        env = rt._dev_local_worker_env(cfg)
    assert env.get("PULUMI_ACCESS_TOKEN") == "pul-test-token-abc123"


def test_worker_env_omits_pulumi_token_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``PULUMI_ACCESS_TOKEN`` is NOT in the env, the var is omitted
    entirely — pulumi surfaces its own "must be set" error rather than the
    worker passing an empty string."""
    monkeypatch.delenv("PULUMI_ACCESS_TOKEN", raising=False)
    cfg = _base_cfg()
    rt = _runtime_with_cfg(cfg)
    with patch.object(rt, "_ensure_dev_local_credentials"):
        env = rt._dev_local_worker_env(cfg)
    assert "PULUMI_ACCESS_TOKEN" not in env, (
        f"env should omit PULUMI_ACCESS_TOKEN when not configured; "
        f"got: {env.get('PULUMI_ACCESS_TOKEN')!r}"
    )
