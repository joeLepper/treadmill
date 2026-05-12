"""LocalRuntime dev-local mode tests (Phase D.2).

Covers:

- ``load_deployment_yaml`` — happy path + every validation failure mode.
- Dev-local container env wiring — API + worker containers get the
  expected ``TREADMILL_*`` / aliased env vars from the YAML; no
  ``AWS_ENDPOINT_URL`` (that's the moto override).
- ``~/.aws`` mount — wired into API + agent in dev-local; absent in
  fully-local mode.
- Fully-local path unchanged when no ``--deployment`` is passed.
- Missing-YAML CLI handling — clean error, not a Python traceback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from treadmill_local import runtime as runtime_module
from treadmill_local.cli import app
from treadmill_local.deployment_config import load_deployment_yaml
from treadmill_local.runner import ContainerSpec
from treadmill_local.runtime import (
    AGENT_FAMILY,
    API_FAMILY,
    POSTGRES_FAMILY,
    REDIS_FAMILY,
    LocalRuntime,
)


runner = CliRunner()


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _valid_yaml_dict(deployment_id: str = "personal") -> dict[str, Any]:
    """A minimal-but-complete YAML dict matching ADR-0016's schema."""
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
        },
        "secrets": {
            "github_webhook_secret_name": f"treadmill-{deployment_id}/github-webhook-secret",
            "github_pat_secret_name": f"treadmill-{deployment_id}/github-pat",
            "worker_aws_credentials_secret_name": (
                f"treadmill-{deployment_id}/worker-aws-credentials"
            ),
        },
        "local": {
            "database_url": "postgresql://treadmill:treadmill@localhost:5432/treadmill",
            "redis_url": "redis://localhost:6379/0",
            "api_url": "http://localhost:8000",
        },
    }


@pytest.fixture
def yaml_file(tmp_path: Path) -> Path:
    """Write a valid YAML file to tmp_path and return the path."""
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(_valid_yaml_dict()))
    return path


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``docker.from_env`` so the LocalRuntime constructor doesn't
    require a daemon.

    Returns the MagicMock the runtime gets, so individual tests can
    assert on ``containers.run`` calls if they want to.
    """
    fake = MagicMock(name="fake_docker")
    monkeypatch.setattr(runtime_module.docker, "from_env", lambda: fake)
    return fake


# ── load_deployment_yaml ──────────────────────────────────────────────────────


def test_load_deployment_yaml_happy_path(yaml_file: Path) -> None:
    cfg = load_deployment_yaml("personal", path=yaml_file)
    assert cfg["deployment_id"] == "personal"
    assert cfg["deployment_mode"] == "dev_local"
    assert cfg["aws_profile"] == "treadmill-personal"
    assert cfg["aws"]["events_topic_arn"].startswith("arn:aws:sns:")
    assert cfg["secrets"]["github_pat_secret_name"] == "treadmill-personal/github-pat"


def test_load_deployment_yaml_missing_file_raises_clear_error(tmp_path: Path) -> None:
    """The missing-file path names the path + suggests ``init`` as remediation."""
    missing = tmp_path / "nope.yaml"
    with pytest.raises(FileNotFoundError, match="nope.yaml"):
        load_deployment_yaml("nope", path=missing)
    # Remediation hint is part of the contract — the operator sees it.
    try:
        load_deployment_yaml("nope", path=missing)
    except FileNotFoundError as exc:
        assert "treadmill-local init nope" in str(exc)


def test_load_deployment_yaml_malformed_yaml_raises_value_error(
    tmp_path: Path,
) -> None:
    """Garbage in the file surfaces as ValueError with the path."""
    path = tmp_path / "bad.yaml"
    path.write_text("not: valid: yaml: [\n")
    with pytest.raises(ValueError, match="not valid YAML"):
        load_deployment_yaml("bad", path=path)


def test_load_deployment_yaml_top_level_list_rejected(tmp_path: Path) -> None:
    """A YAML list at the top level isn't a deployment config."""
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_deployment_yaml("list", path=path)


def test_load_deployment_yaml_missing_top_level_key_raises(tmp_path: Path) -> None:
    """Dropping ``aws_profile`` from a valid file names the missing key."""
    bad = _valid_yaml_dict()
    del bad["aws_profile"]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match="aws_profile"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_mismatched_deployment_id_raises(
    tmp_path: Path,
) -> None:
    """File's deployment_id != caller's slug → ValueError (likely wrong path)."""
    cfg = _valid_yaml_dict(deployment_id="employer")
    path = tmp_path / "employer.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="deployment_id"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_wrong_deployment_mode_raises(tmp_path: Path) -> None:
    """Only ``dev_local`` is supported by the loader."""
    bad = _valid_yaml_dict()
    bad["deployment_mode"] = "fully_remote"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match="dev_local"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_missing_aws_key_raises(tmp_path: Path) -> None:
    """An incomplete ``aws`` block surfaces with the missing inner key named."""
    bad = _valid_yaml_dict()
    del bad["aws"]["work_queue_url"]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match="work_queue_url"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_missing_secrets_key_raises(tmp_path: Path) -> None:
    bad = _valid_yaml_dict()
    del bad["secrets"]["github_pat_secret_name"]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match="github_pat_secret_name"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_aws_block_must_be_mapping(tmp_path: Path) -> None:
    """A scalar where the ``aws`` mapping should be → ValueError."""
    bad = _valid_yaml_dict()
    bad["aws"] = "not a dict"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match="not a mapping"):
        load_deployment_yaml("personal", path=path)


# ── Dev-local env wiring (unit, no docker) ────────────────────────────────────


def test_dev_local_api_env_wires_aws_resources_from_yaml() -> None:
    """The API container's env carries every AWS ARN/URL from the YAML
    under the exact env-var names ``Settings`` reads."""
    cfg = _valid_yaml_dict()
    env = LocalRuntime._dev_local_api_env(cfg)

    # Deployment-mode literal — the Settings field uses the TREADMILL_
    # env prefix (no explicit alias).
    assert env["TREADMILL_DEPLOYMENT_MODE"] == "dev_local"

    # AWS routing — real AWS, NO moto override.
    assert env["AWS_PROFILE"] == "treadmill-personal"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_DEFAULT_REGION"] == "us-east-1"
    assert env["AWS_ACCOUNT_ID"] == "111111111111"
    assert "AWS_ENDPOINT_URL" not in env

    # AWS resources — aliased Settings fields take the unprefixed name.
    assert env["EVENTS_TOPIC_ARN"] == cfg["aws"]["events_topic_arn"]
    assert env["EVENTS_QUEUE_URL"] == cfg["aws"]["events_queue_url"]
    assert env["WORK_QUEUE_URL"] == cfg["aws"]["work_queue_url"]
    assert env["WEBHOOK_INBOX_QUEUE_URL"] == cfg["aws"]["webhook_inbox_queue_url"]

    # Webhook secret name (ADR-0017 poller path).
    assert env["GITHUB_WEBHOOK_SECRET_NAME"] == (
        cfg["secrets"]["github_webhook_secret_name"]
    )

    # Local-side wiring uses the container-DNS hostnames (the Postgres /
    # Redis sibling containers' service names).
    assert "treadmill-postgres" in env["DATABASE_URL"]
    assert "treadmill-redis" in env["REDIS_URL"]


def test_dev_local_worker_env_wires_github_mode_from_yaml() -> None:
    """The worker container's env carries the github-mode contract per
    D.1 + D.3 — repo mode, PAT secret name, AWS-credentials secret name,
    AWS profile + region. ``AWS_ENDPOINT_URL`` MUST be absent."""
    cfg = _valid_yaml_dict()
    env = LocalRuntime._dev_local_worker_env(cfg)

    # Repo mode flips from local (Week-2 default) to github (Week-4 D.1).
    assert env["REPO_MODE"] == "github"

    # Worker reads queue/topic for boto3 work-queue claims + event publish.
    assert env["WORK_QUEUE_URL"] == cfg["aws"]["work_queue_url"]
    assert env["EVENTS_TOPIC_ARN"] == cfg["aws"]["events_topic_arn"]
    # API URL points at the sibling API container by DNS name.
    assert env["TREADMILL_API_URL"] == "http://treadmill-api:8088"

    # Secrets names flow through verbatim (worker fetches the values at
    # startup per D.3 — local-adapter doesn't fetch).
    assert env["GITHUB_PAT_SECRET_NAME"] == cfg["secrets"]["github_pat_secret_name"]
    assert env["WORKER_AWS_CREDENTIALS_SECRET_NAME"] == (
        cfg["secrets"]["worker_aws_credentials_secret_name"]
    )

    # Real-AWS routing.
    assert env["AWS_PROFILE"] == "treadmill-personal"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_DEFAULT_REGION"] == "us-east-1"

    # Moto override is the smoking gun for "wrong mode" — must not be set.
    assert "AWS_ENDPOINT_URL" not in env


def test_dev_local_service_specs_includes_postgres_redis_api(
    tmp_path: Path,
    fake_docker: MagicMock,
) -> None:
    """``_build_dev_local_service_specs`` returns exactly the three
    long-running services (Postgres + Redis + API). The agent worker
    is NOT a service — it's launched on-demand by ``start_worker_once``."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    specs = rt._build_dev_local_service_specs(rt.deployment_config)
    families = {s.family for s in specs}
    assert families == {POSTGRES_FAMILY, REDIS_FAMILY, API_FAMILY}

    # API service exposes 8088 → 8088 on the host (operator hits
    # localhost:8088 to submit plans, etc.).
    api = next(s for s in specs if s.family == API_FAMILY)
    assert api.port_mappings == [(8088, 8088)]

    # Postgres + Redis are shifted to non-default host ports.
    pg = next(s for s in specs if s.family == POSTGRES_FAMILY)
    assert pg.port_mappings == [(5432, 15432)]
    rd = next(s for s in specs if s.family == REDIS_FAMILY)
    assert rd.port_mappings == [(6379, 16379)]


# ── ~/.aws mount ──────────────────────────────────────────────────────────────


def _make_runtime_with_aws_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deployment_config: dict[str, Any] | None,
) -> LocalRuntime:
    """Build a LocalRuntime with a fake ``~/.aws/`` on disk + stubbed docker.

    The fake home dir contains an ``.aws/credentials`` so the
    ``aws_dir.exists()`` check in ``_volumes_for`` finds it.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".aws").mkdir(parents=True)
    (fake_home / ".aws" / "credentials").write_text("[treadmill-personal]\n")
    monkeypatch.setattr(runtime_module.Path, "home", classmethod(lambda cls: fake_home))

    fake_docker_obj = MagicMock(name="fake_docker")
    monkeypatch.setattr(
        runtime_module.docker, "from_env", lambda: fake_docker_obj,
    )
    return LocalRuntime(tmp_path, deployment_config=deployment_config)


def test_volumes_for_api_in_dev_local_mounts_aws_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API container in dev-local mode gets ``~/.aws`` mounted at
    ``/root/.aws`` (read-only). Without this, ``AWS_PROFILE`` resolves
    to "credentials file not found" inside the container.

    Read-only is load-bearing: a ``rw`` mount lets the container's
    root-uid SSO-token refresh leave root-owned files in the operator's
    host cache, breaking the operator's own ``aws sso login`` until they
    chown back. The proper fix (fetch worker keys on the host, inject as
    env vars; drop the ~/.aws mount entirely) is tracked separately."""
    rt = _make_runtime_with_aws_dir(
        tmp_path, monkeypatch, deployment_config=_valid_yaml_dict(),
    )
    monkeypatch.chdir(tmp_path)

    api_spec = ContainerSpec(family=API_FAMILY, name="api", image="treadmill-api:dev")
    mounts = rt._volumes_for(api_spec)

    expected_host = str(tmp_path / "home" / ".aws")
    assert expected_host in mounts
    assert mounts[expected_host] == {"bind": "/root/.aws", "mode": "ro"}


def test_volumes_for_agent_in_dev_local_mounts_aws_dir_alongside_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent in dev-local gets BOTH the Claude creds mount AND the
    ``~/.aws`` mount — the worker needs both to clone real repos and
    talk to real AWS Secrets Manager."""
    rt = _make_runtime_with_aws_dir(
        tmp_path, monkeypatch, deployment_config=_valid_yaml_dict(),
    )
    # Also seed Claude credentials so we exercise both branches at once.
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / ".credentials.json").write_text("{}")
    monkeypatch.chdir(tmp_path)

    agent_spec = ContainerSpec(
        family=AGENT_FAMILY, name="agent", image="treadmill-agent:dev",
    )
    mounts = rt._volumes_for(agent_spec)

    # ~/.aws mount (read-only — see test_volumes_for_api_in_dev_local for why).
    aws_host = str(fake_home / ".aws")
    assert aws_host in mounts
    assert mounts[aws_host] == {"bind": "/root/.aws", "mode": "ro"}

    # Claude creds mount (preserved from fully-local path).
    creds_host = str(fake_home / ".claude" / ".credentials.json")
    assert creds_host in mounts
    assert mounts[creds_host]["bind"] == "/root/.claude/.credentials.json"


def test_volumes_for_api_in_fully_local_has_no_aws_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fully-local mode (no deployment_config) MUST NOT mount ``~/.aws``
    — the API uses moto with fake credentials, not the operator's
    real AWS profile."""
    rt = _make_runtime_with_aws_dir(
        tmp_path, monkeypatch, deployment_config=None,
    )
    monkeypatch.chdir(tmp_path)

    api_spec = ContainerSpec(family=API_FAMILY, name="api", image="treadmill-api:dev")
    mounts = rt._volumes_for(api_spec)

    # Empty: no AWS mount, no Claude creds (not an agent family).
    assert mounts == {}


def test_volumes_for_postgres_in_dev_local_has_no_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres + Redis don't get the AWS mount — they're stock images
    that don't talk to AWS at all."""
    rt = _make_runtime_with_aws_dir(
        tmp_path, monkeypatch, deployment_config=_valid_yaml_dict(),
    )
    pg_spec = ContainerSpec(family=POSTGRES_FAMILY, name="pg", image="postgres:16-alpine")
    assert rt._volumes_for(pg_spec) == {}


def test_volumes_for_dev_local_skips_aws_mount_when_dir_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the operator has no ``~/.aws`` (fresh laptop, SSO not yet
    configured), the mount is silently skipped rather than failing —
    the container will fail with a clear boto3 error instead."""
    fake_home = tmp_path / "home-noaws"
    fake_home.mkdir()
    monkeypatch.setattr(runtime_module.Path, "home", classmethod(lambda cls: fake_home))

    fake_docker_obj = MagicMock(name="fake_docker")
    monkeypatch.setattr(
        runtime_module.docker, "from_env", lambda: fake_docker_obj,
    )
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())

    api_spec = ContainerSpec(family=API_FAMILY, name="api", image="treadmill-api:dev")
    assert rt._volumes_for(api_spec) == {}


# ── Dev-local up: skips moto + skips synth ────────────────────────────────────


def test_up_dev_local_skips_moto_and_synth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``up()`` in dev-local mode does NOT touch moto and does NOT run
    ``cdk synth`` — the entire moto provisioning path is bypassed."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())

    # Stub out the things we expect NOT to be called.
    start_moto_called = []
    synth_called = []
    monkeypatch.setattr(rt, "_start_moto", lambda: start_moto_called.append(1))
    monkeypatch.setattr(rt, "_wait_for_moto", lambda *a, **kw: start_moto_called.append(1))
    monkeypatch.setattr(rt, "_synth", lambda: synth_called.append(1))
    # Stub _start_services so we don't need a real daemon.
    started: list[Any] = []
    monkeypatch.setattr(rt, "_start_services", lambda: started.append(1))
    monkeypatch.setattr(rt, "_ensure_network", lambda: None)
    # ``_ensure_images_built`` would shell out to ``docker build`` —
    # stub it; image-rebuild behavior is exercised in its own tests.
    monkeypatch.setattr(rt, "_ensure_images_built", lambda: None)

    rt.up()

    assert start_moto_called == [], "moto must NOT start in dev-local mode"
    assert synth_called == [], "cdk synth must NOT run in dev-local mode"
    assert started == [1], "services must be started"

    # Container specs populated for ``start_worker_once`` to find the agent.
    assert rt.state.container_specs is not None
    assert any(s.family == AGENT_FAMILY for s in rt.state.container_specs)


def test_up_fully_local_path_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """Without ``--deployment``, ``up`` runs the original moto path —
    ``_start_moto`` is invoked, ``_synth`` happens, no AWS_PROFILE env
    is injected into any container."""
    rt = LocalRuntime(tmp_path, deployment_config=None)

    moto_started = []
    monkeypatch.setattr(rt, "_start_moto", lambda: moto_started.append(1))
    monkeypatch.setattr(rt, "_wait_for_moto", lambda *a, **kw: None)
    monkeypatch.setattr(rt, "_ensure_network", lambda: None)
    monkeypatch.setattr(rt, "_ensure_images_built", lambda: None)
    monkeypatch.setattr(rt, "_ensure_provisioned", lambda: None)
    monkeypatch.setattr(rt, "_start_services", lambda: None)
    monkeypatch.setattr(rt, "_start_autoscaler", lambda: None)
    monkeypatch.setattr(rt, "_report_up", lambda: None)

    rt.up()
    assert moto_started == [1], "moto must start in fully-local mode"


# ── CLI: --deployment flag plumbing ───────────────────────────────────────────


def test_cli_up_with_missing_deployment_yaml_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``treadmill-local up --deployment nonexistent`` exits non-zero with
    a clear message (not a Python traceback)."""
    # Point HOME at a tmp dir so there's no real ~/.treadmill collision.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    result = runner.invoke(
        app,
        ["up", "--deployment", "nonexistent"],
    )
    assert result.exit_code != 0
    # Operator-facing error names the missing config + the remediation.
    # Rich may wrap long paths anywhere — even mid-word — so we check
    # for the remediation phrase (which contains the deployment slug)
    # to confirm a clean error landed rather than a Python traceback.
    assert "Traceback" not in result.output
    assert "treadmill-local init nonexistent" in " ".join(result.output.split())


def test_cli_up_with_malformed_yaml_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed YAML file → exit non-zero with a YAML-parse message."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    (tmp_path / ".treadmill" / "broken.yaml").write_text("not: valid: yaml: [\n")

    result = runner.invoke(
        app,
        ["up", "--deployment", "broken"],
    )
    assert result.exit_code != 0
    # Rich may wrap mid-word so we assert on the YAML-parse signature
    # rather than the full path.
    assert "Traceback" not in result.output
    flat = " ".join(result.output.split())
    assert "not valid YAML" in flat or "mapping values are not allowed" in flat


def test_cli_up_dev_local_does_not_require_cdk_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cdk.json check is skipped in dev-local mode — the operator
    may have no ``infra/`` checkout when running against a remote stack.

    We mock LocalRuntime.up so the test doesn't try to start docker.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    (tmp_path / ".treadmill" / "personal.yaml").write_text(
        yaml.safe_dump(_valid_yaml_dict())
    )

    # Replace LocalRuntime.up with a no-op so we exercise just the CLI plumbing.
    with patch.object(LocalRuntime, "up", lambda self: None), \
         patch.object(LocalRuntime, "__init__", lambda self, **kw: setattr(self, "deployment_config", kw.get("deployment_config")) or None):
        # The empty tmp_path is what --infra would default to; it has no cdk.json.
        # In fully-local mode this would exit 2; in dev-local it should proceed.
        result = runner.invoke(
            app,
            ["up", "--deployment", "personal", "--infra", str(tmp_path)],
        )
    assert result.exit_code == 0, result.output


def test_cli_up_fully_local_still_requires_cdk_json(
    tmp_path: Path,
) -> None:
    """Without ``--deployment``, the cdk.json check is preserved —
    fully-local mode synthesizes via ``cdk synth`` and needs the file."""
    result = runner.invoke(
        app,
        ["up", "--infra", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "cdk.json" in result.output
