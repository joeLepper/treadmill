"""Image-build coverage — the auto-rebuild step in ``up`` + ``run-worker``.

Phase E.1 part-2 surfaced a silent-failure mode: the local-adapter started
``treadmill-api:dev`` / ``treadmill-agent:dev`` containers that were built
weeks earlier and silently ran pre-Phase-A.1 code. The runtime now invokes
``docker build`` before any container references those images. These tests
exercise that wiring without actually shelling out to Docker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from treadmill_local import runtime as runtime_module
from treadmill_local.cli import app
from treadmill_local.runtime import (
    DEV_LOCAL_AGENT_IMAGE,
    DEV_LOCAL_API_IMAGE,
    DEV_LOCAL_DASHBOARD_IMAGE,
    LocalRuntime,
    find_repo_root,
)


runner = CliRunner()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``docker.from_env`` so LocalRuntime constructs without a
    daemon. Tests that exercise ``_ensure_images_built`` only need the
    subprocess module patched — the docker SDK isn't touched."""
    fake = MagicMock(name="fake_docker")
    monkeypatch.setattr(runtime_module.docker, "from_env", lambda: fake)
    return fake


@pytest.fixture
def fake_repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Pin ``find_repo_root`` to a tmp_path so the build invocations'
    ``cwd`` is asserted against a known value (and so tests don't depend
    on the layout of the surrounding checkout)."""
    monkeypatch.setattr(
        runtime_module, "find_repo_root", lambda: tmp_path,
    )
    return tmp_path


def _valid_yaml_dict() -> dict[str, Any]:
    """Minimal valid deployment YAML — re-exports
    ``test_runtime_dev_local._valid_yaml_dict`` shape without the import
    coupling (this module owns the build-step concerns)."""
    return {
        "deployment_id": "personal",
        "deployment_mode": "dev_local",
        "aws_profile": "treadmill-personal",
        "aws_region": "us-east-1",
        "aws_account_id": "111111111111",
        "aws": {
            "events_topic_arn": (
                "arn:aws:sns:us-east-1:111111111111:treadmill-personal-events"
            ),
            "events_queue_url": (
                "https://sqs.us-east-1.amazonaws.com/111111111111/"
                "treadmill-personal-coordination"
            ),
            "work_queue_url": (
                "https://sqs.us-east-1.amazonaws.com/111111111111/"
                "treadmill-personal-work.fifo"
            ),
            "webhook_inbox_queue_url": (
                "https://sqs.us-east-1.amazonaws.com/111111111111/"
                "treadmill-personal-webhook-inbox"
            ),
            "webhook_inbox_dlq_url": (
                "https://sqs.us-east-1.amazonaws.com/111111111111/"
                "treadmill-personal-webhook-inbox-dlq"
            ),
            "webhook_api_url": "https://abc.execute-api.us-east-1.amazonaws.com",
            "deploy_events_queue_url": (
                "https://sqs.us-east-1.amazonaws.com/111111111111/"
                "treadmill-personal-deploy-events"
            ),
            "deploy_events_dlq_url": (
                "https://sqs.us-east-1.amazonaws.com/111111111111/"
                "treadmill-personal-deploy-events-dlq"
            ),
        },
        "secrets": {
            "github_webhook_secret_name": "treadmill-personal/github-webhook-secret",
            "github_pat_secret_name": "treadmill-personal/github-pat",
            "worker_aws_credentials_secret_name": (
                "treadmill-personal/worker-aws-credentials"
            ),
            "api_aws_credentials_secret_name": (
                "treadmill-personal/api-aws-credentials"
            ),
        },
        "local": {
            "database_url": "postgresql://treadmill:treadmill@localhost:5432/treadmill",
            "redis_url": "redis://localhost:6379/0",
            "api_url": "http://localhost:8088",
        },
        # ADR-0018: autoscaler block (defaults — exercised here only for
        # spec completeness; the image-build tests don't depend on values).
        "autoscaler": {"min": 0, "max": 1, "tick_seconds": 5},
    }


# ── find_repo_root ────────────────────────────────────────────────────────────


def test_find_repo_root_resolves_workspace_pyproject() -> None:
    """The real ``find_repo_root`` resolves the Treadmill checkout root
    by walking up from the runtime module path. The result must contain
    a ``pyproject.toml`` with ``[tool.uv.workspace]`` — the marker we
    documented in the function's docstring."""
    root = find_repo_root()
    pyproject = (root / "pyproject.toml").read_text()
    assert "[tool.uv.workspace]" in pyproject
    # Sanity: the two Dockerfile contexts the build step references
    # must exist at the resolved root.
    assert (root / "services" / "api" / "Dockerfile").exists()
    assert (root / "workers" / "agent" / "Dockerfile").exists()


# ── _ensure_images_built: happy path ──────────────────────────────────────────


def test_ensure_images_built_invokes_docker_build_for_each_image(
    tmp_path: Path,
    fake_docker: MagicMock,
    fake_repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ``docker build`` invocation per image lands, with the exact
    argv + cwd each Dockerfile expects.

    - API Dockerfile is context-rooted at ``services/api/`` (its
      ``COPY`` paths are relative to that directory).
    - Agent Dockerfile is context-rooted at the repo root and selected
      via ``-f`` because the agent ``pyproject.toml`` declares
      ``treadmill-api`` as a workspace source — both packages must be
      visible at build time.
    - Dashboard Dockerfile is context-rooted at ``services/dashboard/``
      (ADR-0056: self-contained Node-build → nginx-serve image, no
      cross-package COPYs).
    """
    calls: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    rt._ensure_images_built()

    assert len(calls) == 3, f"expected 3 docker build calls, got {calls}"

    api_call = calls[0]
    assert api_call["cmd"] == [
        "docker", "build", "-t", DEV_LOCAL_API_IMAGE, ".",
    ]
    assert api_call["cwd"] == str(fake_repo_root / "services" / "api")

    agent_call = calls[1]
    assert agent_call["cmd"] == [
        "docker", "build",
        "-t", DEV_LOCAL_AGENT_IMAGE,
        "-f", "workers/agent/Dockerfile",
        ".",
    ]
    assert agent_call["cwd"] == str(fake_repo_root)

    dashboard_call = calls[2]
    assert dashboard_call["cmd"] == [
        "docker", "build", "-t", DEV_LOCAL_DASHBOARD_IMAGE, ".",
    ]
    assert dashboard_call["cwd"] == str(fake_repo_root / "services" / "dashboard")


def test_ensure_images_built_captures_output_on_success(
    tmp_path: Path,
    fake_docker: MagicMock,
    fake_repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a successful build we ``capture_output=True`` so the cached
    "CACHED" wall doesn't drown the rest of the ``up`` progress block.
    The captured streams stay swallowed when returncode is zero."""
    captured_kwargs: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_kwargs.append(kwargs)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="#1 [internal] load build definition\n#2 CACHED\n",
            stderr="",
        )

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    rt._ensure_images_built()

    for kwargs in captured_kwargs:
        assert kwargs.get("capture_output") is True


# ── _ensure_images_built: --no-build skip ─────────────────────────────────────


def test_ensure_images_built_skipped_when_build_images_false(
    tmp_path: Path,
    fake_docker: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_images=False`` (the constructor wiring behind ``--no-build``)
    must short-circuit before any ``docker build`` is invoked. We assert
    by patching ``subprocess.run`` to fail loudly if called at all."""
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be called when --no-build is set")

    monkeypatch.setattr(runtime_module.subprocess, "run", boom)

    rt = LocalRuntime(
        tmp_path,
        deployment_config=_valid_yaml_dict(),
        build_images=False,
    )
    rt._ensure_images_built()  # must not raise


# ── _ensure_images_built: failure surfacing ───────────────────────────────────


def test_ensure_images_built_raises_when_first_build_fails(
    tmp_path: Path,
    fake_docker: MagicMock,
    fake_repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero ``docker build`` exit must raise + abort the up flow.
    The second image's build is not attempted (failing API means the
    agent's workspace-source copy of API would be stale anyway, and
    we want a single clear error)."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=1,
            stdout="Step 3/10 ...\n",
            stderr="error: pip install failed: ResolutionError\n",
        )

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    with pytest.raises(RuntimeError, match=DEV_LOCAL_API_IMAGE):
        rt._ensure_images_built()

    # API build attempted, agent build aborted on first failure.
    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "build", "-t"]
    assert DEV_LOCAL_API_IMAGE in calls[0]


def test_ensure_images_built_raises_when_second_build_fails(
    tmp_path: Path,
    fake_docker: MagicMock,
    fake_repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the agent build fails after the API build succeeds, the
    exception names the failing image so the operator goes straight to
    the right Dockerfile."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if DEV_LOCAL_AGENT_IMAGE in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=2,
                stdout="", stderr="npm install failed\n",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    with pytest.raises(RuntimeError, match=DEV_LOCAL_AGENT_IMAGE):
        rt._ensure_images_built()

    assert len(calls) == 2


# ── up + start_worker_once wiring ─────────────────────────────────────────────


def test_up_dev_local_calls_ensure_images_built_before_starting_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """Ordering is load-bearing: a stale image gets started if the build
    happens after ``_start_services``, defeating the whole point. Lock
    the sequence in: ``_ensure_network`` → ``_ensure_images_built`` →
    ``_start_services``."""
    order: list[str] = []
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    monkeypatch.setattr(rt, "_ensure_network", lambda: order.append("network"))
    monkeypatch.setattr(rt, "_ensure_images_built", lambda: order.append("build"))
    monkeypatch.setattr(rt, "_start_services", lambda: order.append("services"))
    monkeypatch.setattr(rt, "_report_up_dev_local", lambda cfg: None)
    # ADR-0018: dev-local ``up`` also spawns the autoscaler. Stub it so
    # this test only exercises the network → build → services order.
    monkeypatch.setattr(rt, "_start_autoscaler_dev_local", lambda: None)
    # Likewise stub the scheduler, deploy-watcher, and observability
    # spawns (all dev-local ``up`` side-effects) so this test doesn't
    # shell out to subprocesses or ``docker compose``.
    monkeypatch.setattr(rt, "_start_scheduler_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_deploy_watcher_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_observability_dev_local", lambda: None)
    # ADR-0019: ``up`` fetches AWS credentials on the host before building
    # any spec env. Stub the fetch so unit tests don't hit real boto3.
    monkeypatch.setattr(rt, "_ensure_dev_local_credentials", lambda: None)
    rt._worker_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._api_aws_env = {
        "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y",
    }
    rt._github_token = "ghp_test"

    rt.up()
    assert order == ["network", "build", "services"]


def test_up_fully_local_calls_ensure_images_built_before_starting_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """Same ordering invariant for fully-local mode. The build sits
    between network-creation and moto-startup so a missing/broken
    image fails fast before moto + provisioning waste time."""
    order: list[str] = []
    rt = LocalRuntime(tmp_path)
    monkeypatch.setattr(rt, "_ensure_network", lambda: order.append("network"))
    monkeypatch.setattr(rt, "_ensure_images_built", lambda: order.append("build"))
    monkeypatch.setattr(rt, "_start_moto", lambda: order.append("moto"))
    monkeypatch.setattr(rt, "_wait_for_moto", lambda *a, **kw: None)
    monkeypatch.setattr(rt, "_ensure_provisioned", lambda: None)
    monkeypatch.setattr(rt, "_start_services", lambda: order.append("services"))
    monkeypatch.setattr(rt, "_start_autoscaler", lambda: None)
    monkeypatch.setattr(rt, "_report_up", lambda: None)

    rt.up()
    # Build must land between network and moto-start; services after both.
    assert order.index("network") < order.index("build") < order.index("moto")
    assert order.index("build") < order.index("services")


def test_start_worker_once_calls_ensure_images_built(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``run-worker`` against an already-up stack must also rebuild the
    worker image — the operator may have pulled new worker code mid-
    session. Layer cache keeps this near-free when nothing changed."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())

    build_calls: list[int] = []
    monkeypatch.setattr(rt, "_ensure_images_built", lambda: build_calls.append(1))
    # ADR-0019: ``start_worker_once`` in a fresh CLI process fetches AWS
    # credentials on the host before building the agent spec. Stub the
    # fetch so this test doesn't hit real boto3.
    monkeypatch.setattr(rt, "_ensure_dev_local_credentials", lambda: None)
    rt._worker_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._api_aws_env = {
        "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y",
    }
    rt._github_token = "ghp_test"

    # Short-circuit the rest of ``start_worker_once`` so the test stays
    # focused on the build wiring.
    fake_container = MagicMock(name="agent-container")

    def fake_run_container(*_args: Any, **_kwargs: Any) -> Any:
        return fake_container

    monkeypatch.setattr(rt, "_run_container", fake_run_container)

    result = rt.start_worker_once("treadmill-agent")

    assert build_calls == [1]
    assert result is fake_container


# ── CLI: --no-build flag plumbing ─────────────────────────────────────────────


def test_cli_up_default_passes_build_images_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--no-build``, the CLI constructs ``LocalRuntime`` with
    ``build_images=True``. We assert by intercepting the constructor."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    (tmp_path / ".treadmill" / "personal.yaml").write_text(
        yaml.safe_dump(_valid_yaml_dict())
    )

    captured: dict[str, Any] = {}

    def fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self.deployment_config = kwargs.get("deployment_config")
        self.build_images = kwargs.get("build_images", True)

    with patch.object(LocalRuntime, "__init__", fake_init), \
         patch.object(LocalRuntime, "up", lambda self: None):
        result = runner.invoke(
            app,
            ["up", "--deployment", "personal", "--infra", str(tmp_path)],
        )

    assert result.exit_code == 0, result.output
    assert captured["build_images"] is True


def test_cli_up_no_build_flag_passes_build_images_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-build`` flips the constructor kwarg."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    (tmp_path / ".treadmill" / "personal.yaml").write_text(
        yaml.safe_dump(_valid_yaml_dict())
    )

    captured: dict[str, Any] = {}

    def fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self.deployment_config = kwargs.get("deployment_config")
        self.build_images = kwargs.get("build_images", True)

    with patch.object(LocalRuntime, "__init__", fake_init), \
         patch.object(LocalRuntime, "up", lambda self: None):
        result = runner.invoke(
            app,
            [
                "up", "--deployment", "personal",
                "--infra", str(tmp_path), "--no-build",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured["build_images"] is False


def test_cli_run_worker_no_build_flag_passes_build_images_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``--no-build`` flag is also on ``run-worker`` so an operator
    who deliberately wants the existing worker image (e.g., debugging
    a known-good build) can skip the rebuild there too."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    (tmp_path / ".treadmill" / "personal.yaml").write_text(
        yaml.safe_dump(_valid_yaml_dict())
    )

    captured: dict[str, Any] = {}

    def fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self.deployment_config = kwargs.get("deployment_config")
        self.build_images = kwargs.get("build_images", True)

    fake_container = MagicMock(name="container")
    fake_container.short_id = "abc123"
    fake_container.name = "treadmill-worker-treadmill-agent-00001"

    with patch.object(LocalRuntime, "__init__", fake_init), \
         patch.object(LocalRuntime, "start_worker_once", lambda self, family: fake_container):
        result = runner.invoke(
            app,
            [
                "run-worker", "treadmill-agent",
                "--deployment", "personal",
                "--infra", str(tmp_path),
                "--no-build",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured["build_images"] is False


def test_cli_run_worker_default_passes_build_images_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``run-worker`` builds — same auto-rebuild policy as ``up``."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    (tmp_path / ".treadmill" / "personal.yaml").write_text(
        yaml.safe_dump(_valid_yaml_dict())
    )

    captured: dict[str, Any] = {}

    def fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self.deployment_config = kwargs.get("deployment_config")
        self.build_images = kwargs.get("build_images", True)

    fake_container = MagicMock(name="container")
    fake_container.short_id = "abc123"
    fake_container.name = "treadmill-worker-treadmill-agent-00001"

    with patch.object(LocalRuntime, "__init__", fake_init), \
         patch.object(LocalRuntime, "start_worker_once", lambda self, family: fake_container):
        result = runner.invoke(
            app,
            [
                "run-worker", "treadmill-agent",
                "--deployment", "personal",
                "--infra", str(tmp_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured["build_images"] is True
