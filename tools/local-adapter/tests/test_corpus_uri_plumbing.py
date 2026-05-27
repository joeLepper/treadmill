"""TREADMILL_CORPUS_S3_URI worker-env plumbing (ADR-0053 Wave 3).

The agent worker pulls the labeled corpus from S3 via
``tools/load-analysis-corpus.sh``, which reads ``TREADMILL_CORPUS_S3_URI``
from env. The local-adapter's ``_dev_local_worker_env`` lifts the URI
from ``cfg["aws"]["corpus_s3_uri"]`` (an OPERATOR-supplied YAML field —
not a CDK output) into the worker container's env. Absent: env-var
omitted entirely so the optimizer surfaces a clean configuration error
rather than failing deep in boto3.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from treadmill_local.runtime import LocalRuntime


def _runtime_with_cfg(cfg: dict) -> LocalRuntime:
    """Construct a LocalRuntime that won't talk to real AWS/Docker — we only
    exercise the env-building method."""
    from pathlib import Path
    rt = LocalRuntime(
        infra_dir=Path("/fake/infra"),
        deployment_config=cfg,
    )
    # Pre-populate the worker AWS creds attribute the env builder asserts on.
    rt._worker_aws_env = {
        "AWS_ACCESS_KEY_ID": "TEST",
        "AWS_SECRET_ACCESS_KEY": "TEST",
    }
    return rt


def _base_cfg(aws_extra: dict | None = None) -> dict:
    aws = {
        "events_topic_arn": "arn:test",
        "events_queue_url": "https://test/events",
        "work_queue_url": "https://test/work",
        "webhook_inbox_queue_url": "https://test/inbox",
    }
    if aws_extra:
        aws.update(aws_extra)
    return {
        "deployment_id": "test",
        "aws_region": "us-west-2",
        "aws": aws,
        "secrets": {
            "github_pat_secret_name": "test/pat",
        },
    }


def test_worker_env_carries_corpus_uri_when_configured() -> None:
    """When ``aws.corpus_s3_uri`` is set, the worker env carries
    ``TREADMILL_CORPUS_S3_URI`` with the same value."""
    cfg = _base_cfg(aws_extra={
        "corpus_s3_uri": "s3://my-bucket/docs/analysis/",
    })
    rt = _runtime_with_cfg(cfg)
    with patch.object(rt, "_ensure_dev_local_credentials"):
        env = rt._dev_local_worker_env(cfg)
    assert env.get("TREADMILL_CORPUS_S3_URI") == "s3://my-bucket/docs/analysis/"


def test_worker_env_omits_corpus_uri_when_absent() -> None:
    """When ``aws.corpus_s3_uri`` is NOT set, the env var is omitted
    entirely — NOT set to empty string, NOT set to None. The optimizer
    surfaces a clean 'URI not configured' error instead of trying to
    parse an empty URI."""
    cfg = _base_cfg()  # no corpus_s3_uri
    rt = _runtime_with_cfg(cfg)
    with patch.object(rt, "_ensure_dev_local_credentials"):
        env = rt._dev_local_worker_env(cfg)
    assert "TREADMILL_CORPUS_S3_URI" not in env, (
        f"env should omit TREADMILL_CORPUS_S3_URI when not configured; "
        f"got: {env.get('TREADMILL_CORPUS_S3_URI')!r}"
    )
