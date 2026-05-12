"""Worker startup-auth tests.

Covers ``treadmill_agent.startup_auth``:

  * AWS session resolution — collapsed to a single
    ``boto3.Session(region_name=...)`` per ADR-0019. The worker no
    longer fetches its own credentials secret; the local-adapter
    injects the IAM-User keys as env vars before the worker starts.
  * GitHub PAT fetch from Secrets Manager.
  * Handoff to ``gh auth login --with-token`` + ``gh auth setup-git``.
  * Fail-fast behavior on any failure in the chain.
  * The PAT-sentinel leak regression: after startup completes, the
    sentinel value must not be reachable through ``os.environ``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from treadmill_agent import startup_auth
from treadmill_agent.config import Settings
from treadmill_agent.startup_auth import StartupAuthError


_PAT_SENTINEL = "ghp_STARTUP_TEST_SENTINEL_DO_NOT_LEAK_abc789"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        api_url="http://api",
        work_queue_url="http://sqs/q",
        events_topic_arn=None,
        aws_endpoint_url=None,
        aws_region="us-east-1",
        repo_mode="github",
        bare_repos_dir="/tmp/bare",
        workspace_dir="/tmp/ws",
        exit_after_step=True,
        poll_wait_seconds=20,
        claude_credentials_path="/root/.claude/.credentials.json",
        github_pat_secret_name="treadmill-test/github-pat",
    )
    base.update(overrides)
    return Settings(**base)


# ── AWS session resolution ──────────────────────────────────────────────────


def test_resolve_session_returns_region_scoped_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per ADR-0019, ``resolve_worker_aws_session`` collapses to a single
    ``boto3.Session(region_name=...)``. The injected env vars
    (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``) are picked up by
    boto3's default env-var credential chain — no Secrets Manager call
    happens inside the worker, ever."""
    settings = _settings()
    fake_session = object()

    class _FakeBoto3Module:
        class session:
            Session = mock.MagicMock(return_value=fake_session)

        Session = session.Session  # boto3.Session top-level form

    monkeypatch.setattr(startup_auth, "boto3", _FakeBoto3Module)
    session = startup_auth.resolve_worker_aws_session(settings)
    assert session is fake_session
    # Single call, no credentials kwargs — env-var chain is the only
    # credential source.
    _FakeBoto3Module.Session.assert_called_once_with(region_name="us-east-1")
    _, kwargs = _FakeBoto3Module.Session.call_args
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


def test_resolve_session_never_touches_secrets_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for ADR-0019: the worker must NOT call Secrets Manager
    to fetch its own credentials. If it did, the bootstrap-vs-worker
    pattern would be back and the SSO-cache failure mode along with it."""
    settings = _settings()
    secrets_calls: list[Any] = []
    fake_session = mock.MagicMock()
    # Any ``.client(...)`` call records the service name; if Secrets
    # Manager is ever requested we fail loudly.
    fake_session.client.side_effect = lambda svc, *a, **kw: secrets_calls.append(svc) or mock.MagicMock()

    class _FakeBoto3Module:
        class session:
            Session = mock.MagicMock(return_value=fake_session)

        Session = session.Session

    monkeypatch.setattr(startup_auth, "boto3", _FakeBoto3Module)
    startup_auth.resolve_worker_aws_session(settings)
    assert "secretsmanager" not in secrets_calls


# ── GitHub PAT bootstrap ────────────────────────────────────────────────────


def _make_fake_session_returning_pat(pat: str) -> mock.MagicMock:
    """Build a fake boto3 session whose ``.client("secretsmanager")``
    returns a stub that yields ``pat`` on ``get_secret_value``."""
    session = mock.MagicMock()
    secrets = mock.MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": pat}
    session.client.return_value = secrets
    return session


def test_bootstrap_happy_path_fetches_then_pipes_to_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path:

      1. Calls Secrets Manager with the configured secret name.
      2. Pipes the PAT into ``gh auth login --with-token`` via
         ``input=`` (stdin). The PAT must NOT appear in argv or env.
      3. Then runs ``gh auth setup-git`` (no PAT needed by then).

    Both subprocess calls happen in that order, both return zero, and
    the function returns cleanly.
    """
    settings = _settings(github_pat_secret_name="treadmill-test/github-pat")
    session = _make_fake_session_returning_pat(_PAT_SENTINEL)

    calls: list[dict[str, Any]] = []

    def _fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append({
            "argv": argv,
            "input": kwargs.get("input"),
            "env_keys": list(kwargs.get("env", {}).keys()) if kwargs.get("env") else None,
            "capture_output": kwargs.get("capture_output"),
        })
        return mock.MagicMock(returncode=0, stderr=b"", stdout=b"")

    monkeypatch.setattr(startup_auth.subprocess, "run", _fake_run)
    startup_auth.bootstrap_github_auth(settings=settings, aws_session=session)

    session.client.assert_called_once_with("secretsmanager")
    session.client().get_secret_value.assert_called_once_with(
        SecretId="treadmill-test/github-pat",
    )
    assert len(calls) == 2
    # 1st call: gh auth login --with-token, PAT via stdin
    assert calls[0]["argv"] == ["gh", "auth", "login", "--with-token"]
    assert calls[0]["input"] == _PAT_SENTINEL.encode()
    # The PAT must not appear in argv.
    for arg in calls[0]["argv"]:
        assert _PAT_SENTINEL not in arg
    # 2nd call: gh auth setup-git, no input piped
    assert calls[1]["argv"] == ["gh", "auth", "setup-git"]
    assert calls[1]["input"] is None


def test_bootstrap_raises_when_secret_name_unset() -> None:
    """A misconfiguration (repo_mode=github but secret name missing)
    must fail fast at startup, not at first git operation."""
    settings = _settings(github_pat_secret_name=None)
    session = mock.MagicMock()
    with pytest.raises(StartupAuthError, match="GITHUB_PAT_SECRET_NAME"):
        startup_auth.bootstrap_github_auth(settings=settings, aws_session=session)


def test_bootstrap_raises_when_secret_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Secrets Manager call must raise — not silently skip
    auth (which would let the worker continue and 401 on first clone)."""
    settings = _settings(github_pat_secret_name="treadmill-test/missing-pat")
    session = mock.MagicMock()
    secrets = mock.MagicMock()
    secrets.get_secret_value.side_effect = RuntimeError(
        "ResourceNotFoundException: secret not found",
    )
    session.client.return_value = secrets

    with pytest.raises(StartupAuthError, match="failed to fetch GitHub PAT"):
        startup_auth.bootstrap_github_auth(settings=settings, aws_session=session)


def test_bootstrap_raises_when_secret_has_no_secretstring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(github_pat_secret_name="treadmill-test/empty-pat")
    session = mock.MagicMock()
    secrets = mock.MagicMock()
    secrets.get_secret_value.return_value = {}  # no SecretString key
    session.client.return_value = secrets

    with pytest.raises(StartupAuthError, match="no SecretString"):
        startup_auth.bootstrap_github_auth(settings=settings, aws_session=session)


def test_bootstrap_raises_when_gh_auth_login_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(github_pat_secret_name="treadmill-test/github-pat")
    session = _make_fake_session_returning_pat(_PAT_SENTINEL)

    def _fake_run(argv: list[str], **kwargs: Any) -> Any:
        if argv[:3] == ["gh", "auth", "login"]:
            return mock.MagicMock(
                returncode=1, stderr=b"bad token", stdout=b"",
            )
        return mock.MagicMock(returncode=0, stderr=b"", stdout=b"")

    monkeypatch.setattr(startup_auth.subprocess, "run", _fake_run)
    with pytest.raises(StartupAuthError, match="`gh auth login --with-token` exited 1"):
        startup_auth.bootstrap_github_auth(settings=settings, aws_session=session)


def test_bootstrap_raises_when_gh_auth_setup_git_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(github_pat_secret_name="treadmill-test/github-pat")
    session = _make_fake_session_returning_pat(_PAT_SENTINEL)

    def _fake_run(argv: list[str], **kwargs: Any) -> Any:
        if argv[:3] == ["gh", "auth", "setup-git"]:
            return mock.MagicMock(
                returncode=2, stderr=b"helper install failed", stdout=b"",
            )
        return mock.MagicMock(returncode=0, stderr=b"", stdout=b"")

    monkeypatch.setattr(startup_auth.subprocess, "run", _fake_run)
    with pytest.raises(StartupAuthError, match="`gh auth setup-git` exited 2"):
        startup_auth.bootstrap_github_auth(settings=settings, aws_session=session)


def test_bootstrap_does_not_put_pat_in_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``bootstrap_github_auth`` returns, neither the PAT secret
    name nor the PAT value should be in ``os.environ`` (and no env-var
    smelling like ``*TOKEN*`` should have the sentinel value).

    This is the regression for: "we never put the PAT into env" — the
    keyring is the only persistence channel.
    """
    settings = _settings(github_pat_secret_name="treadmill-test/github-pat")
    session = _make_fake_session_returning_pat(_PAT_SENTINEL)
    monkeypatch.setattr(
        startup_auth.subprocess, "run",
        lambda *a, **k: mock.MagicMock(returncode=0, stderr=b"", stdout=b""),
    )
    startup_auth.bootstrap_github_auth(settings=settings, aws_session=session)
    for key, value in os.environ.items():
        assert _PAT_SENTINEL not in value, (
            f"PAT sentinel leaked into env var {key}"
        )


# ── End-to-end: clone uses gh's keyring (no token in URL) ───────────────────


def test_github_mode_clone_after_bootstrap_has_no_token_in_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composition test: after the startup bootstrap "would have"
    populated ``gh``'s keyring (we don't actually exercise the keyring;
    we stub ``git`` to record argv), a subsequent ``git.clone`` in
    github mode must invoke ``git clone https://github.com/<owner>/<repo>.git``
    with no PAT in the URL. The keyring + credential helper are the
    only auth channel."""
    from treadmill_agent import git

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "git.jsonl"
    stub = bin_dir / "git"
    stub.write_text(
        '#!/usr/bin/env python3\n'
        'import json, os, sys\n'
        f'with open({json.dumps(str(log_path))}, "a") as f:\n'
        '    f.write(json.dumps({"argv": sys.argv[1:]}) + "\\n")\n'
        'if len(sys.argv) >= 4 and sys.argv[1] == "clone":\n'
        '    os.makedirs(os.path.join(sys.argv[3], ".git"), exist_ok=True)\n'
        'sys.exit(0)\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GITHUB_PAT", _PAT_SENTINEL)  # worst-case host env

    workspace = tmp_path / "ws"
    workspace.mkdir()
    git.clone(
        repo="owner/test-repo", mode="github",
        bare_repos_dir="/unused", workspace=workspace,
    )

    calls = [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]
    clone_call = next(c for c in calls if c["argv"][0] == "clone")
    url = clone_call["argv"][1]
    assert url == "https://github.com/owner/test-repo.git"
    assert _PAT_SENTINEL not in url
    assert "@" not in url
