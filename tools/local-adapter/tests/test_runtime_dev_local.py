"""LocalRuntime dev-local mode tests (Phase D.2 + ADR-0019).

Covers:

- ``load_deployment_yaml`` — happy path + every validation failure mode.
- Dev-local container env wiring — API + worker containers get the
  expected ``TREADMILL_*`` / aliased env vars from the YAML; no
  ``AWS_ENDPOINT_URL`` (that's the moto override).
- Host-side credential injection per ADR-0019:
  * API container env carries IAM-User keys
    ``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY`` fetched from the
    deployment's api-aws-credentials secret (no ``AWS_PROFILE``,
    no ``AWS_SESSION_TOKEN``).
  * Worker container env carries IAM-User keys
    ``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY`` fetched from the
    deployment's worker-aws-credentials secret (no ``AWS_PROFILE``,
    no ``WORKER_AWS_CREDENTIALS_SECRET_NAME``).
- ``~/.aws`` mount is gone in dev-local — that mount is the SSO-cache
  failure mode ADR-0019 retires.
- Fully-local path unchanged when no ``--deployment`` is passed.
- Missing-YAML CLI handling — clean error, not a Python traceback.
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
from treadmill_local.deployment_config import load_deployment_yaml
from treadmill_local.runner import ContainerSpec
from treadmill_local.runtime import (
    AGENT_FAMILY,
    API_FAMILY,
    DASHBOARD_FAMILY,
    DEV_LOCAL_DASHBOARD_CONTAINER_PORT,
    DEV_LOCAL_DASHBOARD_HOST_PORT,
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
        # ADR-0018: optional autoscaler block. Including the explicit
        # defaults here so fixtures exercise the post-load shape
        # (every consumer reads cfg["autoscaler"] without a nullable check).
        "autoscaler": {
            "min": 0,
            "max": 1,
            "tick_seconds": 5,
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


def _runtime_with_injected_creds(
    tmp_path: Path,
    fake_docker: MagicMock,
    *,
    cfg: dict[str, Any] | None = None,
    worker_creds: dict[str, str] | None = None,
    api_creds: dict[str, str] | None = None,
) -> LocalRuntime:
    """Build a LocalRuntime with pre-populated credential env dicts so
    the env-builder unit tests don't have to mock boto3 + Secrets Manager.

    Mirrors what ``_ensure_dev_local_credentials`` would do after a real
    fetch. The helper is in this file (not a shared fixture) because
    only the env-wiring tests need it; the integration paths exercise
    the fetch separately."""
    if cfg is None:
        cfg = _valid_yaml_dict()
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    rt._worker_aws_env = worker_creds or {
        "AWS_ACCESS_KEY_ID": "AKIA-WORKER-TEST",
        "AWS_SECRET_ACCESS_KEY": "worker-secret-test",
    }
    rt._api_aws_env = api_creds or {
        "AWS_ACCESS_KEY_ID": "AKIA-API-TEST",
        "AWS_SECRET_ACCESS_KEY": "api-secret-test",
    }
    rt._github_token = "ghp_test_token_placeholder"
    return rt


def test_dev_local_api_env_wires_aws_resources_from_yaml(
    tmp_path: Path, fake_docker: MagicMock,
) -> None:
    """The API container's env carries every AWS ARN/URL from the YAML
    under the exact env-var names ``Settings`` reads, plus the
    API IAM-User credentials injected per ADR-0019."""
    cfg = _valid_yaml_dict()
    rt = _runtime_with_injected_creds(tmp_path, fake_docker, cfg=cfg)
    env = rt._dev_local_api_env(cfg)

    # Deployment-mode literal — the Settings field uses the TREADMILL_
    # env prefix (no explicit alias).
    assert env["TREADMILL_DEPLOYMENT_MODE"] == "dev_local"

    # AWS routing — real AWS, NO moto override.
    # AWS_PROFILE is GONE per ADR-0019: env-var creds replace profile.
    assert "AWS_PROFILE" not in env
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_DEFAULT_REGION"] == "us-east-1"
    assert env["AWS_ACCOUNT_ID"] == "111111111111"
    assert "AWS_ENDPOINT_URL" not in env

    # API IAM-User keys (per ADR-0019). No session token — IAM-User
    # credentials are long-lived and don't carry a token.
    assert env["AWS_ACCESS_KEY_ID"] == "AKIA-API-TEST"
    assert env["AWS_SECRET_ACCESS_KEY"] == "api-secret-test"
    assert "AWS_SESSION_TOKEN" not in env

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


def test_dev_local_worker_env_wires_github_mode_from_yaml(
    tmp_path: Path, fake_docker: MagicMock,
) -> None:
    """The worker container's env carries the github-mode contract —
    repo mode, PAT secret name, AWS region — plus the IAM-User keys
    fetched on the host and injected per ADR-0019.
    ``AWS_ENDPOINT_URL``, ``AWS_PROFILE``, and
    ``WORKER_AWS_CREDENTIALS_SECRET_NAME`` MUST all be absent."""
    cfg = _valid_yaml_dict()
    rt = _runtime_with_injected_creds(tmp_path, fake_docker, cfg=cfg)
    env = rt._dev_local_worker_env(cfg)

    # Repo mode flips from local (Week-2 default) to github (Week-4 D.1).
    assert env["REPO_MODE"] == "github"

    # Worker reads queue/topic for boto3 work-queue claims + event publish.
    assert env["WORK_QUEUE_URL"] == cfg["aws"]["work_queue_url"]
    assert env["EVENTS_TOPIC_ARN"] == cfg["aws"]["events_topic_arn"]
    # API URL points at the sibling API container by DNS name.
    assert env["TREADMILL_API_URL"] == "http://treadmill-api:8088"

    # PAT secret name flows through verbatim (worker still fetches PAT
    # at startup).
    assert env["GITHUB_PAT_SECRET_NAME"] == cfg["secrets"]["github_pat_secret_name"]

    # Per ADR-0019: worker no longer needs the AWS credentials secret
    # name (the local-adapter fetched the value and injected the keys).
    assert "WORKER_AWS_CREDENTIALS_SECRET_NAME" not in env

    # AWS routing — real AWS, no moto override, no profile.
    assert "AWS_PROFILE" not in env
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_DEFAULT_REGION"] == "us-east-1"

    # Worker IAM-User keys injected from the host fetch.
    assert env["AWS_ACCESS_KEY_ID"] == "AKIA-WORKER-TEST"
    assert env["AWS_SECRET_ACCESS_KEY"] == "worker-secret-test"
    # Worker IAM-User keys are long-lived — no session token.
    assert "AWS_SESSION_TOKEN" not in env

    # Moto override is the smoking gun for "wrong mode" — must not be set.
    assert "AWS_ENDPOINT_URL" not in env


# ── OTel exporter wiring (ADR-0020) ───────────────────────────────────────────


def _yaml_with_collector(collector: str | None) -> dict[str, Any]:
    """Helper: copy of ``_valid_yaml_dict`` with the OTel collector field
    explicitly set (or removed when ``None``). The base fixture omits
    the field; tests that exercise the OTel-wiring branch need to
    inject it deliberately so the assertion is unambiguous."""
    cfg = _valid_yaml_dict()
    if collector is None:
        cfg["aws"].pop("observability_collector_endpoint", None)
    else:
        cfg["aws"]["observability_collector_endpoint"] = collector
    return cfg


def test_dev_local_api_env_injects_otlp_protocol_when_collector_set(
    tmp_path: Path, fake_docker: MagicMock,
) -> None:
    """When the collector endpoint is wired in, the API container's env
    pairs ``OTEL_EXPORTER_OTLP_ENDPOINT`` with
    ``OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf``. The catch-all
    unsuffixed protocol var applies to all signals (traces/metrics/logs)
    — we deliberately don't set the signal-specific variants.

    The protocol/port mismatch this prevents: the OTel Python SDK
    defaults to gRPC (port 4317), but the collector listens on 4318
    for HTTP. Without this var the SDK speaks gRPC against the HTTP
    port and every export fails with ``StatusCode.UNAVAILABLE``.
    """
    cfg = _yaml_with_collector("http://treadmill-otel-collector:4318")
    rt = _runtime_with_injected_creds(tmp_path, fake_docker, cfg=cfg)
    env = rt._dev_local_api_env(cfg)

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "http://treadmill-otel-collector:4318"
    )
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    # Signal-specific protocol vars are NOT set — the unsuffixed
    # catch-all is sufficient.
    assert "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL" not in env
    assert "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL" not in env
    assert "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL" not in env


def test_dev_local_worker_env_injects_otlp_protocol_when_collector_set(
    tmp_path: Path, fake_docker: MagicMock,
) -> None:
    """Worker mirrors the API: collector endpoint + ``http/protobuf``
    protocol var are injected together."""
    cfg = _yaml_with_collector("http://treadmill-otel-collector:4318")
    rt = _runtime_with_injected_creds(tmp_path, fake_docker, cfg=cfg)
    env = rt._dev_local_worker_env(cfg)

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "http://treadmill-otel-collector:4318"
    )
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL" not in env
    assert "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL" not in env
    assert "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL" not in env


def test_dev_local_api_env_omits_otlp_vars_when_collector_absent(
    tmp_path: Path, fake_docker: MagicMock,
) -> None:
    """Operators running without the observability stack rely on the
    SDK being a silent no-op. The protocol var must be gated on the
    SAME ``if collector:`` block as the endpoint — setting it alone
    could surprise the SDK."""
    cfg = _yaml_with_collector(None)
    rt = _runtime_with_injected_creds(tmp_path, fake_docker, cfg=cfg)
    env = rt._dev_local_api_env(cfg)

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert "OTEL_EXPORTER_OTLP_PROTOCOL" not in env


def test_dev_local_worker_env_omits_otlp_vars_when_collector_absent(
    tmp_path: Path, fake_docker: MagicMock,
) -> None:
    """Worker counterpart to the no-collector API test."""
    cfg = _yaml_with_collector(None)
    rt = _runtime_with_injected_creds(tmp_path, fake_docker, cfg=cfg)
    env = rt._dev_local_worker_env(cfg)

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert "OTEL_EXPORTER_OTLP_PROTOCOL" not in env


def test_dev_local_otlp_collector_bare_host_port_gets_http_scheme(
    tmp_path: Path, fake_docker: MagicMock,
) -> None:
    """When the YAML carries a bare ``host:port`` (no scheme), the
    env-builder prepends ``http://`` AND still sets the protocol var.
    This is the path a CFN-output-derived value takes."""
    cfg = _yaml_with_collector("treadmill-otel-collector:4318")
    rt = _runtime_with_injected_creds(tmp_path, fake_docker, cfg=cfg)
    api_env = rt._dev_local_api_env(cfg)
    worker_env = rt._dev_local_worker_env(cfg)

    for env in (api_env, worker_env):
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
            "http://treadmill-otel-collector:4318"
        )
        assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"


def test_dev_local_service_specs_includes_postgres_redis_api_dashboard(
    tmp_path: Path,
    fake_docker: MagicMock,
) -> None:
    """``_build_dev_local_service_specs`` returns the four long-running
    services (Postgres + Redis + API + Dashboard). The agent worker is
    NOT a service — it's launched on-demand by ``start_worker_once``.

    ADR-0056 added the dashboard as a separate static-served service so
    the operator dashboard's release cycle decouples from the API's."""
    rt = _runtime_with_injected_creds(tmp_path, fake_docker)
    specs = rt._build_dev_local_service_specs(rt.deployment_config)
    families = {s.family for s in specs}
    assert families == {
        POSTGRES_FAMILY,
        REDIS_FAMILY,
        API_FAMILY,
        DASHBOARD_FAMILY,
    }

    # API service exposes 8088 → 8088 on the host (operator hits
    # localhost:8088 to submit plans, etc.).
    api = next(s for s in specs if s.family == API_FAMILY)
    assert api.port_mappings == [(8088, 8088)]

    # Postgres + Redis are shifted to non-default host ports.
    pg = next(s for s in specs if s.family == POSTGRES_FAMILY)
    assert pg.port_mappings == [(5432, 15432)]
    rd = next(s for s in specs if s.family == REDIS_FAMILY)
    assert rd.port_mappings == [(6379, 16379)]

    # Dashboard exposes nginx (80) → 5174 on the host (one above the
    # Vite dev-server port 5173 so both can coexist during UI iteration).
    dash = next(s for s in specs if s.family == DASHBOARD_FAMILY)
    assert dash.port_mappings == [
        (DEV_LOCAL_DASHBOARD_CONTAINER_PORT, DEV_LOCAL_DASHBOARD_HOST_PORT)
    ]


# ── recreate_api_container (deploy-watcher auto-deploy, ADR-0024) ────────────


def test_recreate_api_container_force_removes_existing_and_runs_new(
    tmp_path: Path,
    fake_docker: MagicMock,
) -> None:
    """``recreate_api_container`` must force-remove any existing
    ``treadmill-api`` container (regardless of status — including
    ``running``, which ``_start_service_container``'s normal path
    skips as already-OK) and then ``docker run`` a new container from
    ``treadmill-api:dev``.

    This is what swaps the live container to the freshly-built image
    on a ``services/api/**`` PR merge. ``docker restart`` (the
    pre-ADR-0024 watcher behavior) re-ran the OLD container's image,
    so api code + migrations never went live."""
    import docker as docker_lib

    rt = _runtime_with_injected_creds(tmp_path, fake_docker)

    existing = MagicMock(name="existing_api_container")
    existing.status = "running"
    fake_docker.containers.get.return_value = existing
    # ``_ensure_image`` checks the image is present; satisfy it so the
    # path doesn't try to pull a ``:dev`` tag (which is local-only).
    fake_docker.images.get.return_value = MagicMock()

    rt.recreate_api_container()

    # Old container force-removed (status==running would normally skip).
    existing.remove.assert_called_once_with(force=True)

    # New container started from the dev image with the API family name,
    # publishing 8088 1:1 on the same docker network ``up`` uses.
    fake_docker.containers.run.assert_called_once()
    call = fake_docker.containers.run.call_args
    assert call.args[0] == "treadmill-api:dev"
    assert call.kwargs["name"] == API_FAMILY
    assert call.kwargs["network"] == "treadmill-local"
    assert call.kwargs["ports"] == {"8088/tcp": 8088}

    # Sanity: nothing in this path issues a ``docker restart`` shell-out —
    # the recreate goes through docker-py, not subprocess. (The
    # subprocess-level guard against ``docker restart`` lives in the
    # deploy-watcher tests; this assertion guards the runtime helper.)
    _ = docker_lib  # quiet "imported but unused" under strict linters


def test_recreate_api_container_when_no_existing_container(
    tmp_path: Path,
    fake_docker: MagicMock,
) -> None:
    """A first deploy after ``down`` (no existing ``treadmill-api``
    container on the host) still runs the new container — the NotFound
    on ``containers.get`` is swallowed and the run proceeds."""
    import docker as docker_lib

    rt = _runtime_with_injected_creds(tmp_path, fake_docker)
    fake_docker.containers.get.side_effect = docker_lib.errors.NotFound(
        "no such container"
    )
    fake_docker.images.get.return_value = MagicMock()

    rt.recreate_api_container()

    fake_docker.containers.run.assert_called_once()
    assert (
        fake_docker.containers.run.call_args.args[0] == "treadmill-api:dev"
    )


# ── recreate_dashboard_container (deploy-watcher auto-deploy, ADR-0056) ──────


def test_recreate_dashboard_container_force_removes_existing_and_runs_new(
    tmp_path: Path,
    fake_docker: MagicMock,
) -> None:
    """``recreate_dashboard_container`` mirrors the API recreate path for
    ``services/dashboard/**`` PR merges: force-remove any existing
    ``treadmill-dashboard`` container (regardless of status — including
    ``running``, which ``_start_service_container``'s normal path skips as
    already-OK) and ``docker run`` a new one from
    ``treadmill-dashboard:dev``.

    Without this swap a merged dashboard PR is a silent no-op — the same
    failure mode ADR-0024 retired on the API side."""
    import docker as docker_lib

    rt = _runtime_with_injected_creds(tmp_path, fake_docker)

    existing = MagicMock(name="existing_dashboard_container")
    existing.status = "running"
    fake_docker.containers.get.return_value = existing
    fake_docker.images.get.return_value = MagicMock()

    # _ensure_images_built shells out to ``docker build`` — patch it so
    # the unit test doesn't try to invoke real docker. The build path is
    # exercised by the existing _ensure_images_built tests.
    with patch.object(rt, "_ensure_images_built"):
        rt.recreate_dashboard_container()

    # Old container force-removed (status==running would normally skip).
    existing.remove.assert_called_once_with(force=True)

    # New container started from the dev image with the dashboard family
    # name, publishing nginx (80) → 5174 on the same docker network ``up``
    # uses (same spec _build_dashboard_service_spec returns to ``up``).
    fake_docker.containers.run.assert_called_once()
    call = fake_docker.containers.run.call_args
    assert call.args[0] == "treadmill-dashboard:dev"
    assert call.kwargs["name"] == DASHBOARD_FAMILY
    assert call.kwargs["network"] == "treadmill-local"
    assert call.kwargs["ports"] == {
        f"{DEV_LOCAL_DASHBOARD_CONTAINER_PORT}/tcp": DEV_LOCAL_DASHBOARD_HOST_PORT,
    }

    _ = docker_lib  # quiet "imported but unused" under strict linters


# ── ~/.aws mount: gone in dev-local (ADR-0019) ──────────────────────────────


def _make_runtime_with_aws_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deployment_config: dict[str, Any] | None,
) -> LocalRuntime:
    """Build a LocalRuntime with a fake ``~/.aws/`` on disk + stubbed docker.

    Used to prove the ``~/.aws`` mount is **not** added in dev-local
    even when the operator has a working SSO profile — per ADR-0019 the
    mount class of bugs is closed by the host-side env-var injection
    path, regardless of whether ``~/.aws`` exists.
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


def test_no_aws_mount_in_dev_local_for_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per ADR-0019: the API container in dev-local does NOT mount
    ``~/.aws``. API's IAM-User keys are fetched from Secrets Manager on
    the host and injected as ``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY``
    env vars on the container (no ``AWS_SESSION_TOKEN``).

    Even when ``~/.aws`` exists on the host (the normal case), the
    mount must not be added — the mount itself is the failure mode
    we're retiring."""
    rt = _make_runtime_with_aws_dir(
        tmp_path, monkeypatch, deployment_config=_valid_yaml_dict(),
    )
    monkeypatch.chdir(tmp_path)

    api_spec = ContainerSpec(family=API_FAMILY, name="api", image="treadmill-api:dev")
    mounts = rt._volumes_for(api_spec)

    # No ~/.aws mount, no /root/.aws bind, no operator-home references.
    expected_aws_host = str(tmp_path / "home" / ".aws")
    assert expected_aws_host not in mounts
    assert not any(m["bind"] == "/root/.aws" for m in mounts.values())
    # API has no other dev-local volumes, so mounts is empty.
    assert mounts == {}


def test_no_aws_mount_in_dev_local_for_agent_only_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per ADR-0019: the agent worker in dev-local does NOT mount
    ``~/.aws``. The IAM-User keys are fetched on the host and injected
    as env vars (see worker env-wiring tests). The agent still gets
    its Claude credentials + bare-repos mounts — those are unrelated
    to the AWS credential path."""
    rt = _make_runtime_with_aws_dir(
        tmp_path, monkeypatch, deployment_config=_valid_yaml_dict(),
    )
    # Seed Claude credentials so we can verify the agent mount survives
    # while the AWS mount stays gone.
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / ".credentials.json").write_text("{}")
    monkeypatch.chdir(tmp_path)

    agent_spec = ContainerSpec(
        family=AGENT_FAMILY, name="agent", image="treadmill-agent:dev",
    )
    mounts = rt._volumes_for(agent_spec)

    # ~/.aws mount is gone.
    aws_host = str(fake_home / ".aws")
    assert aws_host not in mounts
    assert not any(m["bind"] == "/root/.aws" for m in mounts.values())

    # Claude creds mount survives — the worker still needs Claude.
    creds_host = str(fake_home / ".claude" / ".credentials.json")
    assert creds_host in mounts
    assert mounts[creds_host]["bind"] == "/root/.claude/.credentials.json"


def test_volumes_for_api_in_fully_local_has_no_aws_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fully-local mode (no deployment_config) never had the ``~/.aws``
    mount — moto uses fake credentials. This test stays as a regression
    guard so fully-local mode doesn't accidentally pick up the mount."""
    rt = _make_runtime_with_aws_dir(
        tmp_path, monkeypatch, deployment_config=None,
    )
    monkeypatch.chdir(tmp_path)

    api_spec = ContainerSpec(family=API_FAMILY, name="api", image="treadmill-api:dev")
    mounts = rt._volumes_for(api_spec)

    # Empty: no AWS mount, no Claude creds (not an agent family).
    assert mounts == {}


def test_volumes_for_postgres_in_dev_local_mounts_deployment_scoped_named_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres gets a deployment-scoped named volume so DB state
    survives ``down`` + ``up`` cycles — smokes can resume after an
    SSO refresh instead of losing workflow_runs / events / tasks."""
    rt = _make_runtime_with_aws_dir(
        tmp_path, monkeypatch, deployment_config=_valid_yaml_dict(),
    )
    pg_spec = ContainerSpec(family=POSTGRES_FAMILY, name="pg", image="postgres:16-alpine")
    assert rt._volumes_for(pg_spec) == {
        "treadmill-personal-postgres-data": {
            "bind": "/var/lib/postgresql/data",
            "mode": "rw",
        }
    }


# ── Host-side credential fetch (ADR-0019) ────────────────────────────────────


def test_fetch_worker_credentials_returns_env_var_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_fetch_worker_credentials`` fetches the worker IAM-User keys
    from Secrets Manager (using the operator's profile) and returns a
    dict shaped for direct injection as env vars on the worker
    container — ``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY``.

    The secret name comes from the deployment YAML's
    ``secrets.worker_aws_credentials_secret_name``; the payload is JSON
    of shape ``{"aws_access_key_id": ..., "aws_secret_access_key": ...}``
    (matching what ADR-0016 Q16.c standardizes for the IAM-User keys)."""
    cfg = _valid_yaml_dict()

    import json
    fake_secrets_client = MagicMock()
    fake_secrets_client.get_secret_value.return_value = {
        "SecretString": json.dumps({
            "aws_access_key_id": "AKIAFAKEWORKER",
            "aws_secret_access_key": "worker-secret-from-secrets-manager",
        }),
    }
    fake_session = MagicMock()
    fake_session.client.return_value = fake_secrets_client

    boto3_session_calls: list[dict[str, Any]] = []

    def _fake_boto3_session(**kwargs: Any) -> Any:
        boto3_session_calls.append(kwargs)
        return fake_session

    monkeypatch.setattr(runtime_module.boto3, "Session", _fake_boto3_session)

    env = LocalRuntime._fetch_worker_credentials(cfg)

    # Boto3 session was built using the operator's profile + region.
    assert boto3_session_calls == [
        {"profile_name": "treadmill-personal", "region_name": "us-east-1"},
    ]
    # Secrets Manager called with the secret name from the YAML.
    fake_secrets_client.get_secret_value.assert_called_once_with(
        SecretId="treadmill-personal/worker-aws-credentials",
    )
    # The returned dict carries exactly the AWS env-var keys the
    # worker's boto3 reads via the standard env chain.
    assert env == {
        "AWS_ACCESS_KEY_ID": "AKIAFAKEWORKER",
        "AWS_SECRET_ACCESS_KEY": "worker-secret-from-secrets-manager",
    }


def test_fetch_worker_credentials_raises_when_secret_payload_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret whose payload is missing one of the required keys must
    fail loudly — degraded creds would let the container start with a
    half-resolved boto3 chain and 403 later."""
    cfg = _valid_yaml_dict()
    fake_secrets_client = MagicMock()
    fake_secrets_client.get_secret_value.return_value = {
        "SecretString": '{"aws_access_key_id": "AKIAFAKE"}',  # missing secret
    }
    fake_session = MagicMock()
    fake_session.client.return_value = fake_secrets_client
    monkeypatch.setattr(
        runtime_module.boto3, "Session", lambda **kw: fake_session,
    )

    with pytest.raises(RuntimeError, match="missing aws_access_key_id / aws_secret_access_key"):
        LocalRuntime._fetch_worker_credentials(cfg)


def test_fetch_api_credentials_returns_env_var_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_fetch_api_credentials`` fetches the API IAM-User keys
    from Secrets Manager (using the operator's profile) and returns a
    dict shaped for direct injection as env vars on the API
    container — ``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY``.

    The secret name comes from the deployment YAML's
    ``secrets.api_aws_credentials_secret_name``; the payload is JSON
    of shape ``{"aws_access_key_id": ..., "aws_secret_access_key": ...}``
    (matching what ADR-0016 Q16.c standardizes for the IAM-User keys)."""
    cfg = _valid_yaml_dict()

    import json
    fake_secrets_client = MagicMock()
    fake_secrets_client.get_secret_value.return_value = {
        "SecretString": json.dumps({
            "aws_access_key_id": "AKIAFAKEAPI",
            "aws_secret_access_key": "api-secret-from-secrets-manager",
        }),
    }
    fake_session = MagicMock()
    fake_session.client.return_value = fake_secrets_client

    boto3_session_calls: list[dict[str, Any]] = []

    def _fake_boto3_session(**kwargs: Any) -> Any:
        boto3_session_calls.append(kwargs)
        return fake_session

    monkeypatch.setattr(runtime_module.boto3, "Session", _fake_boto3_session)

    env = LocalRuntime._fetch_api_credentials(cfg)

    # Boto3 session was built using the operator's profile + region.
    assert boto3_session_calls == [
        {"profile_name": "treadmill-personal", "region_name": "us-east-1"},
    ]
    # Secrets Manager called with the secret name from the YAML.
    fake_secrets_client.get_secret_value.assert_called_once_with(
        SecretId="treadmill-personal/api-aws-credentials",
    )
    # The returned dict carries exactly the AWS env-var keys the
    # API's boto3 reads via the standard env chain.
    assert env == {
        "AWS_ACCESS_KEY_ID": "AKIAFAKEAPI",
        "AWS_SECRET_ACCESS_KEY": "api-secret-from-secrets-manager",
    }


def test_fetch_api_credentials_raises_when_secret_has_no_secret_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the API credentials secret has no SecretString (operator
    forgot to populate it), raise a clear RuntimeError naming the secret
    and the operator action needed."""
    cfg = _valid_yaml_dict()
    fake_secrets_client = MagicMock()
    fake_secrets_client.get_secret_value.return_value = {}
    fake_session = MagicMock()
    fake_session.client.return_value = fake_secrets_client
    monkeypatch.setattr(
        runtime_module.boto3, "Session", lambda **kw: fake_session,
    )

    with pytest.raises(RuntimeError) as exc_info:
        LocalRuntime._fetch_api_credentials(cfg)
    assert "has no SecretString" in str(exc_info.value)
    assert "aws iam create-access-key" in str(exc_info.value)
    assert "aws secretsmanager put-secret-value" in str(exc_info.value)


def test_fetch_api_credentials_raises_when_secret_payload_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret whose payload is missing one of the required keys must
    fail loudly — degraded creds would let the container start with a
    half-resolved boto3 chain and 403 later."""
    cfg = _valid_yaml_dict()
    fake_secrets_client = MagicMock()
    fake_secrets_client.get_secret_value.return_value = {
        "SecretString": '{"aws_access_key_id": "AKIAFAKE"}',  # missing secret
    }
    fake_session = MagicMock()
    fake_session.client.return_value = fake_secrets_client
    monkeypatch.setattr(
        runtime_module.boto3, "Session", lambda **kw: fake_session,
    )

    with pytest.raises(RuntimeError, match="missing aws_access_key_id / aws_secret_access_key"):
        LocalRuntime._fetch_api_credentials(cfg)


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
    # Per ADR-0018: dev-local ``up`` also spawns the autoscaler. Stub it
    # so this test only exercises the moto/synth bypass; the
    # autoscaler-subprocess wiring has its own focused tests below.
    autoscaler_started: list[Any] = []
    monkeypatch.setattr(
        rt, "_start_autoscaler_dev_local",
        lambda: autoscaler_started.append(1),
    )
    # Stub scheduler + deploy-watcher + observability so they don't spawn
    # subprocesses or shell out to docker compose.
    monkeypatch.setattr(rt, "_start_scheduler_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_deploy_watcher_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_observability_dev_local", lambda: None)
    # Per ADR-0019: ``up`` fetches AWS credentials on the host. Stub
    # the fetch so we don't hit real boto3/Secrets Manager in unit tests.
    rt._worker_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._api_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._github_token = "ghp_test_token_placeholder"

    rt.up()

    assert start_moto_called == [], "moto must NOT start in dev-local mode"
    assert synth_called == [], "cdk synth must NOT run in dev-local mode"
    assert started == [1], "services must be started"
    assert autoscaler_started == [1], "autoscaler must start in dev-local mode"

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


# ── Autoscaler in dev-local (ADR-0018) ────────────────────────────────────────


def test_start_autoscaler_dev_local_spawns_subprocess_with_expected_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``_start_autoscaler_dev_local`` invokes ``subprocess.Popen`` with
    ``python -m treadmill_local.autoscaler`` and an env carrying the
    YAML-derived autoscaler config + the deployment id (so the
    subprocess entrypoint can rebuild a dev-local LocalRuntime per
    ADR-0019).

    Notably the env does NOT carry ``AWS_ENDPOINT_URL`` — that's the
    moto override; dev-local talks to real AWS endpoints.
    """
    cfg = _valid_yaml_dict()
    cfg["autoscaler"] = {"min": 0, "max": 3, "tick_seconds": 7}
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    # No PID file present → autoscaler spawns.
    monkeypatch.chdir(tmp_path)
    # Clear any inherited endpoint override so the assertion below
    # exercises the runtime's explicit pop, not the bare absence.
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://example.local:5001")
    monkeypatch.setenv("AWS_PROFILE", "treadmill-from-env")

    popen_calls: list[dict[str, Any]] = []

    class _FakeProc:
        pid = 4242

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeProc:
        popen_calls.append({"args": args, "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(runtime_module.subprocess, "Popen", _fake_popen)

    rt._start_autoscaler_dev_local()

    assert len(popen_calls) == 1
    call = popen_calls[0]
    # Subprocess command: python -m treadmill_local.autoscaler.
    cmd = call["args"][0]
    assert cmd[1:] == ["-m", "treadmill_local.autoscaler"]

    env = call["kwargs"]["env"]
    # Per ADR-0018: autoscaler config flows from the YAML.
    assert env["TREADMILL_AUTOSCALER_FAMILY"] == "treadmill-agent"
    assert env["TREADMILL_AUTOSCALER_QUEUE_URL"] == cfg["aws"]["work_queue_url"]
    assert env["TREADMILL_AUTOSCALER_MIN"] == "0"
    assert env["TREADMILL_AUTOSCALER_MAX"] == "3"
    assert env["TREADMILL_AUTOSCALER_TICK_SECONDS"] == "7"
    # The deployment id is the load-bearing new env var: subprocess
    # branches on it to construct a dev-local LocalRuntime.
    assert env["TREADMILL_AUTOSCALER_DEPLOYMENT_ID"] == cfg["deployment_id"]
    # AWS routing — region from YAML, profile inherited from parent env.
    assert env["AWS_DEFAULT_REGION"] == cfg["aws_region"]
    assert env["AWS_PROFILE"] == "treadmill-from-env"
    # Moto override MUST NOT leak through — dev-local hits real AWS.
    assert "AWS_ENDPOINT_URL" not in env

    # PID file written for later teardown.
    assert (tmp_path / ".treadmill-local" / "autoscaler.pid").read_text().strip() == "4242"


def test_start_autoscaler_dev_local_uses_yaml_profile_when_env_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When ``AWS_PROFILE`` is not in the parent env, the runtime falls
    back to ``cfg['aws_profile']`` so the subprocess still has a
    profile-resolvable session."""
    cfg = _valid_yaml_dict()
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 5151

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeProc:
        captured["env"] = kwargs["env"]
        return _FakeProc()

    monkeypatch.setattr(runtime_module.subprocess, "Popen", _fake_popen)
    rt._start_autoscaler_dev_local()
    assert captured["env"]["AWS_PROFILE"] == cfg["aws_profile"]


def test_up_dev_local_with_no_autoscaler_flag_skips_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When ``start_autoscaler=False`` (the ``--no-autoscaler`` CLI flag),
    ``_up_dev_local`` does NOT spawn the subprocess."""
    rt = LocalRuntime(
        tmp_path,
        deployment_config=_valid_yaml_dict(),
        start_autoscaler=False,
    )

    monkeypatch.setattr(rt, "_ensure_network", lambda: None)
    monkeypatch.setattr(rt, "_ensure_images_built", lambda: None)
    monkeypatch.setattr(rt, "_start_services", lambda: None)
    monkeypatch.setattr(rt, "_start_scheduler_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_deploy_watcher_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_observability_dev_local", lambda: None)
    rt._worker_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._api_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._github_token = "ghp_test_token_placeholder"

    spawn_calls: list[Any] = []
    monkeypatch.setattr(
        rt, "_start_autoscaler_dev_local",
        lambda: spawn_calls.append(1),
    )

    rt.up()
    assert spawn_calls == [], "autoscaler must NOT spawn when --no-autoscaler is set"


def test_cli_up_no_autoscaler_flag_propagates_to_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-autoscaler`` on ``up`` translates to ``start_autoscaler=False``
    on the runtime constructor."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    (tmp_path / ".treadmill" / "personal.yaml").write_text(
        yaml.safe_dump(_valid_yaml_dict())
    )

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self.deployment_config = kwargs.get("deployment_config")

    with patch.object(LocalRuntime, "up", lambda self: None), \
         patch.object(LocalRuntime, "__init__", _fake_init):
        result = runner.invoke(
            app,
            [
                "up", "--deployment", "personal",
                "--infra", str(tmp_path),
                "--no-autoscaler",
            ],
        )
    assert result.exit_code == 0, result.output
    assert captured["start_autoscaler"] is False


def test_stop_autoscaler_sigterms_pid_and_cleans_up_pid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``_stop_autoscaler`` (shared between fully-local and dev-local)
    SIGTERMs the PID stored in the PID file and removes the file."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / ".treadmill-local"
    state_dir.mkdir()
    pid_file = state_dir / "autoscaler.pid"
    pid_file.write_text("9999")

    # Simulate "alive then dead": first poll returns alive (so we send
    # SIGTERM), the second returns dead (so we don't escalate to KILL).
    pid_alive_calls = [True, False]
    monkeypatch.setattr(
        LocalRuntime, "_pid_alive",
        staticmethod(lambda pid: pid_alive_calls.pop(0) if pid_alive_calls else False),
    )

    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        runtime_module.os, "kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    rt._stop_autoscaler()

    # SIGTERM (signal 15) was sent exactly once.
    import signal as _signal
    assert kill_calls == [(9999, _signal.SIGTERM)]
    # PID file cleaned up.
    assert not pid_file.exists()


def test_stop_autoscaler_no_pid_file_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When no PID file is present, ``_stop_autoscaler`` is a noop —
    it does not call ``os.kill`` and does not raise."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    monkeypatch.chdir(tmp_path)

    kill_calls: list[Any] = []
    monkeypatch.setattr(
        runtime_module.os, "kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    rt._stop_autoscaler()  # Must not raise.
    assert kill_calls == []


# ── YAML loader: autoscaler block (ADR-0018) ──────────────────────────────────


def test_load_deployment_yaml_fills_autoscaler_defaults_when_absent(
    tmp_path: Path,
) -> None:
    """An older YAML file without the ``autoscaler:`` block still loads —
    defaults (min=0, max=1, tick_seconds=5) fill in per ADR-0018."""
    cfg = _valid_yaml_dict()
    del cfg["autoscaler"]
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(cfg))

    loaded = load_deployment_yaml("personal", path=path)
    assert loaded["autoscaler"] == {"min": 0, "max": 1, "tick_seconds": 5}


def test_load_deployment_yaml_accepts_partial_autoscaler_block(
    tmp_path: Path,
) -> None:
    """Only ``max`` set → ``min`` + ``tick_seconds`` fill from defaults."""
    cfg = _valid_yaml_dict()
    cfg["autoscaler"] = {"max": 4}
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(cfg))

    loaded = load_deployment_yaml("personal", path=path)
    assert loaded["autoscaler"] == {"min": 0, "max": 4, "tick_seconds": 5}


def test_load_deployment_yaml_rejects_min_greater_than_max(
    tmp_path: Path,
) -> None:
    cfg = _valid_yaml_dict()
    cfg["autoscaler"] = {"min": 5, "max": 2, "tick_seconds": 1}
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="autoscaler.max"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_rejects_negative_min(tmp_path: Path) -> None:
    cfg = _valid_yaml_dict()
    cfg["autoscaler"] = {"min": -1, "max": 1, "tick_seconds": 5}
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="autoscaler.min"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_rejects_zero_tick_seconds(tmp_path: Path) -> None:
    cfg = _valid_yaml_dict()
    cfg["autoscaler"] = {"min": 0, "max": 1, "tick_seconds": 0}
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="tick_seconds"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_rejects_negative_tick_seconds(tmp_path: Path) -> None:
    cfg = _valid_yaml_dict()
    cfg["autoscaler"] = {"min": 0, "max": 1, "tick_seconds": -3}
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="tick_seconds"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_rejects_non_int_autoscaler_value(
    tmp_path: Path,
) -> None:
    cfg = _valid_yaml_dict()
    cfg["autoscaler"] = {"min": "zero", "max": 1, "tick_seconds": 5}
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="expected an int"):
        load_deployment_yaml("personal", path=path)


def test_load_deployment_yaml_rejects_non_mapping_autoscaler(
    tmp_path: Path,
) -> None:
    cfg = _valid_yaml_dict()
    cfg["autoscaler"] = "not a dict"
    path = tmp_path / "personal.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="not a mapping"):
        load_deployment_yaml("personal", path=path)


# ── Autoscaler subprocess entrypoint: dev-local branch ────────────────────────


def test_autoscaler_main_branches_on_deployment_id_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``TREADMILL_AUTOSCALER_DEPLOYMENT_ID`` is set, the autoscaler
    subprocess's ``main()`` constructs ``LocalRuntime`` with the parsed
    deployment_config (so ``start_worker_once`` triggers the dev-local
    credential injection per ADR-0019).

    We patch ``LocalRuntime.__init__`` and ``Autoscaler.run`` so the
    test exercises only the wiring, not the loop or docker."""
    import treadmill_local.autoscaler as autoscaler_module

    # Set the dev-local env-var signature the entrypoint reads.
    monkeypatch.setenv("TREADMILL_INFRA_DIR", str(tmp_path))
    monkeypatch.setenv("TREADMILL_AUTOSCALER_FAMILY", "treadmill-agent")
    monkeypatch.setenv(
        "TREADMILL_AUTOSCALER_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/111111111111/treadmill-personal-work.fifo",
    )
    monkeypatch.setenv("TREADMILL_AUTOSCALER_MIN", "0")
    monkeypatch.setenv("TREADMILL_AUTOSCALER_MAX", "2")
    monkeypatch.setenv("TREADMILL_AUTOSCALER_TICK_SECONDS", "5")
    monkeypatch.setenv("TREADMILL_AUTOSCALER_DEPLOYMENT_ID", "personal")
    # Pin the rotating log file to tmp_path so main()'s logging setup
    # doesn't create state in the test's working directory.
    monkeypatch.setenv(
        "TREADMILL_AUTOSCALER_LOG_FILE", str(tmp_path / "autoscaler.log"),
    )
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    # Replace LocalRuntime.__init__ so we don't need a real docker daemon.
    init_calls: list[dict[str, Any]] = []

    def _fake_runtime_init(self: Any, **kwargs: Any) -> None:
        init_calls.append(kwargs)
        self.deployment_config = kwargs.get("deployment_config")

    monkeypatch.setattr(
        runtime_module.LocalRuntime, "__init__", _fake_runtime_init,
    )

    # Patch ``Autoscaler.run`` so the loop exits immediately.
    monkeypatch.setattr(
        autoscaler_module.Autoscaler, "run", lambda self: None,
    )

    # Patch ``load_deployment_yaml`` so the entrypoint doesn't try to
    # read a real YAML from $HOME.
    from treadmill_local import deployment_config as dc
    monkeypatch.setattr(
        dc, "load_deployment_yaml",
        lambda deployment_id, path=None: _valid_yaml_dict(
            deployment_id=deployment_id,
        ),
    )

    # Stub docker.from_env and boto3.client so the entrypoint's
    # callable construction doesn't need a real daemon or network.
    monkeypatch.setattr(
        autoscaler_module, "main",
        autoscaler_module.main,  # keep the real main
    )

    import docker as _docker
    monkeypatch.setattr(_docker, "from_env", lambda: MagicMock())
    import boto3 as _boto3
    monkeypatch.setattr(_boto3, "client", lambda *a, **kw: MagicMock())

    rc = autoscaler_module.main()
    assert rc == 0

    # LocalRuntime was constructed with the deployment_config parsed
    # from the YAML — that's the load-bearing branch behavior.
    assert len(init_calls) == 1
    assert init_calls[0]["deployment_config"]["deployment_id"] == "personal"


def test_autoscaler_main_legacy_path_when_deployment_id_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``TREADMILL_AUTOSCALER_DEPLOYMENT_ID`` is unset, the
    subprocess falls back to the fully-local path:
    ``LocalRuntime(infra_dir=infra_dir)`` with no deployment_config
    (moto endpoint + dummy creds inherited via env).
    """
    import treadmill_local.autoscaler as autoscaler_module

    monkeypatch.setenv("TREADMILL_INFRA_DIR", str(tmp_path))
    monkeypatch.setenv("TREADMILL_AUTOSCALER_FAMILY", "treadmill-agent")
    monkeypatch.setenv(
        "TREADMILL_AUTOSCALER_QUEUE_URL",
        "http://localhost:5001/000000000000/work",
    )
    monkeypatch.setenv("TREADMILL_AUTOSCALER_MIN", "0")
    monkeypatch.setenv("TREADMILL_AUTOSCALER_MAX", "1")
    monkeypatch.delenv("TREADMILL_AUTOSCALER_DEPLOYMENT_ID", raising=False)
    # Pin the rotating log file to tmp_path so main()'s logging setup
    # doesn't create state in the test's working directory.
    monkeypatch.setenv(
        "TREADMILL_AUTOSCALER_LOG_FILE", str(tmp_path / "autoscaler.log"),
    )

    init_calls: list[dict[str, Any]] = []

    def _fake_runtime_init(self: Any, **kwargs: Any) -> None:
        init_calls.append(kwargs)
        self.deployment_config = kwargs.get("deployment_config")

    monkeypatch.setattr(
        runtime_module.LocalRuntime, "__init__", _fake_runtime_init,
    )
    monkeypatch.setattr(
        autoscaler_module.Autoscaler, "run", lambda self: None,
    )

    import docker as _docker
    monkeypatch.setattr(_docker, "from_env", lambda: MagicMock())
    import boto3 as _boto3
    monkeypatch.setattr(_boto3, "client", lambda *a, **kw: MagicMock())

    rc = autoscaler_module.main()
    assert rc == 0

    # Legacy path: no deployment_config passed.
    assert len(init_calls) == 1
    assert init_calls[0].get("deployment_config") is None


# ── redeploy() ───────────────────────────────────────────────────────────────


def test_redeploy_runs_cdk_then_cycles_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``redeploy()`` shells out to ``cdk deploy`` with the right args,
    then calls ``down`` then ``up`` in order."""
    rt = _runtime_with_injected_creds(tmp_path, fake_docker)

    # Track call order across cdk/down/up.
    order: list[str] = []
    cdk_calls: list[list[str]] = []

    def fake_subprocess_run(cmd, *, cwd, env, check):
        cdk_calls.append(cmd)
        order.append("cdk")
        # Simulate success.
        return MagicMock(returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(rt, "down", lambda: order.append("down"))
    monkeypatch.setattr(rt, "up", lambda: order.append("up"))

    rt.redeploy()

    assert order == ["cdk", "down", "up"], (
        f"expected cdk → down → up, got {order!r}"
    )
    assert len(cdk_calls) == 1
    cmd = cdk_calls[0]
    assert cmd[0:3] == ["cdk", "deploy", "TreadmillPersonalCloudLite"]
    assert "--context" in cmd and "mode=dev_local" in cmd
    assert "deployment_id=personal" in cmd
    assert "--profile" in cmd and "treadmill-personal" in cmd
    assert "--require-approval" in cmd and "never" in cmd


def test_redeploy_no_cdk_skips_deploy_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``redeploy(skip_cdk=True)`` does NOT run cdk; still cycles."""
    rt = _runtime_with_injected_creds(tmp_path, fake_docker)

    order: list[str] = []
    cdk_calls: list[Any] = []

    def fake_subprocess_run(cmd, *, cwd, env, check):
        cdk_calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(rt, "down", lambda: order.append("down"))
    monkeypatch.setattr(rt, "up", lambda: order.append("up"))

    rt.redeploy(skip_cdk=True)

    assert order == ["down", "up"]
    assert cdk_calls == [], "cdk deploy must not run when skip_cdk=True"


def test_redeploy_aborts_when_cdk_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """If ``cdk deploy`` fails, ``redeploy`` raises SystemExit before
    touching down/up — the operator gets a half-running stack with a
    clear error, not a half-cycled one."""
    rt = _runtime_with_injected_creds(tmp_path, fake_docker)

    order: list[str] = []

    def fake_subprocess_run(cmd, *, cwd, env, check):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(rt, "down", lambda: order.append("down"))
    monkeypatch.setattr(rt, "up", lambda: order.append("up"))

    with pytest.raises(SystemExit) as exc_info:
        rt.redeploy()
    assert exc_info.value.code == 1
    assert order == [], "down/up must not run after a cdk failure"


# ── Deploy-watcher lifecycle ──────────────────────────────────────────────────


def test_start_deploy_watcher_dev_local_spawns_subprocess_with_expected_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``_start_deploy_watcher_dev_local`` invokes ``subprocess.Popen`` with
    ``python -m treadmill_local.deploy_watcher`` and an env carrying the
    deployment ID (so the subprocess loads the right YAML), AWS routing
    vars, and GITHUB_TOKEN (injected from the host-fetched credential).

    Notably the env does NOT carry ``AWS_ENDPOINT_URL`` — that's the
    moto override; dev-local talks to real AWS endpoints.
    """
    cfg = _valid_yaml_dict()
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    rt._github_token = "ghp_injected_token"
    # Pre-set the AWS credential caches so ``_ensure_dev_local_credentials``
    # short-circuits and does not try to load the operator's local
    # ``treadmill-personal`` boto3 profile (which doesn't exist in CI).
    rt._worker_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._api_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    monkeypatch.chdir(tmp_path)
    # Clear any inherited endpoint override so the assertion below
    # exercises the runtime's explicit pop, not the bare absence.
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://example.local:5001")
    monkeypatch.setenv("AWS_PROFILE", "treadmill-from-env")

    popen_calls: list[dict[str, Any]] = []

    class _FakeProc:
        pid = 7777

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeProc:
        popen_calls.append({"args": args, "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(runtime_module.subprocess, "Popen", _fake_popen)
    # The spawn path reads (owner, repo) from the local checkout's origin
    # remote via parse_github_origin → subprocess.run. The Popen patch
    # above intercepts that call too, so stub the parser directly to keep
    # this test focused on the env-shaping assertion below.
    monkeypatch.setattr(
        runtime_module, "parse_github_origin", lambda _root: ("joeLepper", "treadmill")
    )

    rt._start_deploy_watcher_dev_local()

    assert len(popen_calls) == 1
    call = popen_calls[0]
    # Subprocess command: python -m treadmill_local.deploy_watcher.
    cmd = call["args"][0]
    assert cmd[1:] == ["-m", "treadmill_local.deploy_watcher"]

    env = call["kwargs"]["env"]
    # Deployment ID is the load-bearing var: subprocess reads the YAML via it.
    assert env["TREADMILL_DEPLOY_WATCHER_DEPLOYMENT_ID"] == cfg["deployment_id"]
    # AWS routing — region from YAML, profile inherited from parent env.
    assert env["AWS_DEFAULT_REGION"] == cfg["aws_region"]
    assert env["AWS_PROFILE"] == "treadmill-from-env"
    # GITHUB_TOKEN injected from the host-fetched credential, not from env.
    assert env["GITHUB_TOKEN"] == "ghp_injected_token"
    # GitHub repo coordinates derived from origin so the watcher can call
    # the PR-files API without operator-set env vars.
    assert env["GITHUB_OWNER"] == "joeLepper"
    assert env["GITHUB_REPO"] == "treadmill"
    assert "TREADMILL_REPO_ROOT" in env
    # Moto override MUST NOT leak through — dev-local hits real AWS.
    assert "AWS_ENDPOINT_URL" not in env

    # PID file written for later teardown.
    assert (tmp_path / ".treadmill-local" / "deploy-watcher.pid").read_text().strip() == "7777"


def test_start_deploy_watcher_dev_local_uses_yaml_profile_when_env_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When ``AWS_PROFILE`` is not in the parent env, the runtime falls
    back to ``cfg['aws_profile']`` so the subprocess still has a
    profile-resolvable session."""
    cfg = _valid_yaml_dict()
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    rt._github_token = "ghp_test"
    # See sibling test: pre-set AWS env caches so the credential fetch
    # is skipped (CI has no ``treadmill-personal`` boto3 profile).
    rt._worker_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._api_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 8888

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeProc:
        captured["env"] = kwargs["env"]
        return _FakeProc()

    monkeypatch.setattr(runtime_module.subprocess, "Popen", _fake_popen)
    # See sibling test: the global Popen patch intercepts the git-remote
    # subprocess.run inside parse_github_origin too, so stub the parser.
    monkeypatch.setattr(
        runtime_module, "parse_github_origin", lambda _root: ("joeLepper", "treadmill")
    )
    rt._start_deploy_watcher_dev_local()
    assert captured["env"]["AWS_PROFILE"] == cfg["aws_profile"]


def test_up_dev_local_with_no_deploy_watcher_flag_skips_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When ``start_deploy_watcher=False`` (the ``--no-deploy-watcher``
    CLI flag), ``_up_dev_local`` does NOT spawn the subprocess."""
    rt = LocalRuntime(
        tmp_path,
        deployment_config=_valid_yaml_dict(),
        start_deploy_watcher=False,
    )

    monkeypatch.setattr(rt, "_ensure_network", lambda: None)
    monkeypatch.setattr(rt, "_ensure_images_built", lambda: None)
    monkeypatch.setattr(rt, "_start_services", lambda: None)
    monkeypatch.setattr(rt, "_start_autoscaler_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_scheduler_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_observability_dev_local", lambda: None)
    rt._worker_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._api_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._github_token = "ghp_test_token_placeholder"

    spawn_calls: list[Any] = []
    monkeypatch.setattr(
        rt, "_start_deploy_watcher_dev_local",
        lambda: spawn_calls.append(1),
    )

    rt.up()
    assert spawn_calls == [], "deploy watcher must NOT spawn when --no-deploy-watcher is set"


def test_cli_up_no_deploy_watcher_flag_propagates_to_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-deploy-watcher`` on ``up`` translates to
    ``start_deploy_watcher=False`` on the runtime constructor."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    (tmp_path / ".treadmill" / "personal.yaml").write_text(
        yaml.safe_dump(_valid_yaml_dict())
    )

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self.deployment_config = kwargs.get("deployment_config")

    with patch.object(LocalRuntime, "up", lambda self: None), \
         patch.object(LocalRuntime, "__init__", _fake_init):
        result = runner.invoke(
            app,
            [
                "up", "--deployment", "personal",
                "--infra", str(tmp_path),
                "--no-deploy-watcher",
            ],
        )
    assert result.exit_code == 0, result.output
    assert captured["start_deploy_watcher"] is False


def test_stop_deploy_watcher_sigterms_pid_and_cleans_up_pid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``_stop_deploy_watcher`` SIGTERMs the PID stored in the PID file
    and removes the file."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / ".treadmill-local"
    state_dir.mkdir()
    pid_file = state_dir / "deploy-watcher.pid"
    pid_file.write_text("6666")

    # Simulate "alive then dead".
    pid_alive_calls = [True, False]
    monkeypatch.setattr(
        LocalRuntime, "_pid_alive",
        staticmethod(lambda pid: pid_alive_calls.pop(0) if pid_alive_calls else False),
    )

    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        runtime_module.os, "kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    rt._stop_deploy_watcher()

    import signal as _signal
    assert kill_calls == [(6666, _signal.SIGTERM)]
    assert not pid_file.exists()


def test_stop_deploy_watcher_no_pid_file_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When no PID file is present, ``_stop_deploy_watcher`` is a noop."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    monkeypatch.chdir(tmp_path)

    kill_calls: list[Any] = []
    monkeypatch.setattr(
        runtime_module.os, "kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    rt._stop_deploy_watcher()  # Must not raise.
    assert kill_calls == []


def test_down_sigterms_deploy_watcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``down`` calls ``_stop_deploy_watcher``, which SIGTERMs the watcher
    process and removes the PID file."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / ".treadmill-local"
    state_dir.mkdir()
    pid_file = state_dir / "deploy-watcher.pid"
    pid_file.write_text("5555")

    pid_alive_calls = [True, False]
    monkeypatch.setattr(
        LocalRuntime, "_pid_alive",
        staticmethod(lambda pid: pid_alive_calls.pop(0) if pid_alive_calls else False),
    )

    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        runtime_module.os, "kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    # Stub out everything else down() does.
    monkeypatch.setattr(rt, "_stop_autoscaler", lambda: None)
    monkeypatch.setattr(rt, "_stop_scheduler", lambda: None)
    monkeypatch.setattr(rt, "_stop_observability", lambda: None)
    monkeypatch.setattr(rt, "_stop_managed_containers", lambda: None)
    monkeypatch.setattr(rt, "_remove_network", lambda: None)

    rt.down()

    import signal as _signal
    assert (5555, _signal.SIGTERM) in kill_calls
    assert not pid_file.exists()


# ── Observability stack lifecycle (ADR-0043) ──────────────────────────────────


def test_start_observability_dev_local_invokes_docker_compose_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``_start_observability_dev_local`` shells out to
    ``docker compose -f docker-compose.yml -f docker-compose.local.yml up -d``
    so the OTel collector + Loki + Prometheus + Tempo + Grafana stack
    comes up (per ADR-0043).
    """
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    # No prior container → start should proceed.
    fake_docker.containers.get.side_effect = runtime_module.docker.errors.NotFound(
        "no container"
    )

    runs: list[tuple[list[str], dict[str, Any]]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        runs.append((cmd, kwargs))
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", _fake_run)

    rt._start_observability_dev_local()

    assert len(runs) == 1
    cmd, _kwargs = runs[0]
    # Compose CLI invocation with both base and override files.
    assert cmd[0:2] == ["docker", "compose"]
    # Order matters: base file first, local override second (so the
    # override wins on conflicts).
    assert cmd.count("-f") == 2
    f_indexes = [i for i, tok in enumerate(cmd) if tok == "-f"]
    base_path = cmd[f_indexes[0] + 1]
    local_path = cmd[f_indexes[1] + 1]
    assert base_path.endswith("infra/observability/docker-compose.yml"), base_path
    assert local_path.endswith(
        "infra/observability/docker-compose.local.yml"
    ), local_path
    # Detached: returns control after bringing the stack up.
    assert cmd[-2:] == ["up", "-d"]


# ── Configurable Grafana host port (port-3000 collision fix) ─────────────────


def test_start_observability_dev_local_passes_grafana_host_port_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """The compose subprocess receives ``GRAFANA_HOST_PORT`` derived
    from ``aws.observability_grafana_port``. Compose substitutes that
    env var into ``docker-compose.local.yml``'s grafana ports binding,
    so the host port the operator browses matches what was bound.
    """
    cfg = _valid_yaml_dict()
    cfg["aws"]["observability_grafana_port"] = 3001
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    fake_docker.containers.get.side_effect = runtime_module.docker.errors.NotFound(
        "no container"
    )

    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", _fake_run)

    rt._start_observability_dev_local()

    env = captured["env"]
    assert env is not None, "must pass an env dict so compose sees GRAFANA_HOST_PORT"
    assert env.get("GRAFANA_HOST_PORT") == "3001", (
        f"expected GRAFANA_HOST_PORT=3001, got {env.get('GRAFANA_HOST_PORT')!r}"
    )


def test_start_observability_dev_local_grafana_port_default_when_yaml_field_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When the YAML omits ``aws.observability_grafana_port`` (older
    YAMLs pre-dating this knob), the runtime falls back to 3001 — the
    chosen dev-local default. The fallback is the runtime's job, not
    just compose's ``${GRAFANA_HOST_PORT:-3001}`` shell default, so the
    Python-side log line and the compose binding agree on the same
    number."""
    cfg = _valid_yaml_dict()
    # No observability_grafana_port key — the absent case.
    assert "observability_grafana_port" not in cfg["aws"]
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    fake_docker.containers.get.side_effect = runtime_module.docker.errors.NotFound(
        "no container"
    )

    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", _fake_run)

    rt._start_observability_dev_local()

    assert captured["env"]["GRAFANA_HOST_PORT"] == str(
        runtime_module.OBSERVABILITY_GRAFANA_HOST_PORT_DEFAULT
    )
    assert runtime_module.OBSERVABILITY_GRAFANA_HOST_PORT_DEFAULT == 3001


def test_start_observability_dev_local_grafana_port_custom_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """Operator-chosen value (e.g., 3002 when both 3000 AND 3001 are
    bound on the laptop) propagates through into compose's env."""
    cfg = _valid_yaml_dict()
    cfg["aws"]["observability_grafana_port"] = 3002
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    fake_docker.containers.get.side_effect = runtime_module.docker.errors.NotFound(
        "no container"
    )

    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", _fake_run)

    rt._start_observability_dev_local()

    assert captured["env"]["GRAFANA_HOST_PORT"] == "3002"


def test_start_observability_dev_local_inherits_existing_environ(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """The compose env must extend ``os.environ`` (not replace it), so
    operator-set env vars like ``DOCKER_HOST`` or AWS creds still reach
    the compose subprocess. We assert one non-Treadmill env var carries
    through."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///custom/docker.sock")
    cfg = _valid_yaml_dict()
    cfg["aws"]["observability_grafana_port"] = 3001
    rt = LocalRuntime(tmp_path, deployment_config=cfg)
    fake_docker.containers.get.side_effect = runtime_module.docker.errors.NotFound(
        "no container"
    )

    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(runtime_module.subprocess, "run", _fake_run)

    rt._start_observability_dev_local()

    env = captured["env"]
    assert env["DOCKER_HOST"] == "unix:///custom/docker.sock"
    assert env["GRAFANA_HOST_PORT"] == "3001"


def test_start_observability_dev_local_is_noop_when_otel_already_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When the OTel collector container is already running,
    ``_start_observability_dev_local`` does NOT re-invoke docker compose.
    """
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    # Container exists and is running → idempotent skip.
    existing = MagicMock()
    existing.status = "running"
    fake_docker.containers.get.return_value = existing

    runs: list[list[str]] = []
    monkeypatch.setattr(
        runtime_module.subprocess, "run",
        lambda cmd, **kwargs: runs.append(cmd),
    )

    rt._start_observability_dev_local()

    assert runs == [], "must skip compose up when stack is already running"


def test_up_dev_local_with_no_observability_flag_skips_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When ``start_observability=False`` (the ``--no-observability``
    CLI flag), ``_up_dev_local`` does NOT spawn the compose stack."""
    rt = LocalRuntime(
        tmp_path,
        deployment_config=_valid_yaml_dict(),
        start_observability=False,
    )

    monkeypatch.setattr(rt, "_ensure_network", lambda: None)
    monkeypatch.setattr(rt, "_ensure_images_built", lambda: None)
    monkeypatch.setattr(rt, "_start_services", lambda: None)
    monkeypatch.setattr(rt, "_start_autoscaler_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_scheduler_dev_local", lambda: None)
    monkeypatch.setattr(rt, "_start_deploy_watcher_dev_local", lambda: None)
    rt._worker_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._api_aws_env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    rt._github_token = "ghp_test_token_placeholder"

    spawn_calls: list[Any] = []
    monkeypatch.setattr(
        rt, "_start_observability_dev_local",
        lambda: spawn_calls.append(1),
    )

    rt.up()
    assert spawn_calls == [], (
        "observability must NOT spawn when --no-observability is set"
    )


def test_cli_up_no_observability_flag_propagates_to_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-observability`` on ``up`` translates to
    ``start_observability=False`` on the runtime constructor."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".treadmill").mkdir()
    (tmp_path / ".treadmill" / "personal.yaml").write_text(
        yaml.safe_dump(_valid_yaml_dict())
    )

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self.deployment_config = kwargs.get("deployment_config")

    with patch.object(LocalRuntime, "up", lambda self: None), \
         patch.object(LocalRuntime, "__init__", _fake_init):
        result = runner.invoke(
            app,
            [
                "up", "--deployment", "personal",
                "--infra", str(tmp_path),
                "--no-observability",
            ],
        )
    assert result.exit_code == 0, result.output
    assert captured["start_observability"] is False


def test_stop_observability_invokes_docker_compose_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``_stop_observability`` runs ``docker compose down`` (without
    ``-v``, so named volumes survive) when the stack is up."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    # Stack is up → tear it down.
    existing = MagicMock()
    existing.status = "running"
    fake_docker.containers.get.return_value = existing

    runs: list[list[str]] = []
    monkeypatch.setattr(
        runtime_module.subprocess, "run",
        lambda cmd, **kwargs: runs.append(cmd) or subprocess.CompletedProcess(
            args=cmd, returncode=0
        ),
    )

    rt._stop_observability()

    assert len(runs) == 1
    cmd = runs[0]
    assert cmd[0:2] == ["docker", "compose"]
    assert cmd[-1] == "down"
    # Critical: must NOT pass -v / --volumes — the named volumes
    # (treadmill-loki-data, etc.) should survive a down/up cycle so
    # operator metrics + traces persist across restarts.
    assert "-v" not in cmd
    assert "--volumes" not in cmd


def test_stop_observability_is_noop_when_stack_not_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """When the OTel collector container is absent,
    ``_stop_observability`` is a noop — no docker compose call."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    fake_docker.containers.get.side_effect = runtime_module.docker.errors.NotFound(
        "no container"
    )

    runs: list[list[str]] = []
    monkeypatch.setattr(
        runtime_module.subprocess, "run",
        lambda cmd, **kwargs: runs.append(cmd),
    )

    rt._stop_observability()
    assert runs == []


def test_down_stops_observability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """``down`` invokes ``_stop_observability`` so a clean teardown
    removes the compose stack alongside the host subprocesses."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    monkeypatch.chdir(tmp_path)

    stop_calls: list[Any] = []
    monkeypatch.setattr(
        rt, "_stop_observability", lambda: stop_calls.append(1)
    )
    # Stub the other teardown steps so this test stays focused on
    # the observability hook.
    monkeypatch.setattr(rt, "_stop_autoscaler", lambda: None)
    monkeypatch.setattr(rt, "_stop_scheduler", lambda: None)
    monkeypatch.setattr(rt, "_stop_deploy_watcher", lambda: None)
    monkeypatch.setattr(rt, "_stop_managed_containers", lambda: None)
    monkeypatch.setattr(rt, "_remove_network", lambda: None)

    rt.down()

    assert stop_calls == [1], "down must tear down the observability stack"


def test_start_observability_dev_local_raises_on_compose_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_docker: MagicMock,
) -> None:
    """If ``docker compose up`` returns non-zero (e.g., port conflict),
    ``_start_observability_dev_local`` propagates the failure so the
    operator sees the fail-fast clearly rather than a silently broken
    stack. Verifies the brief's hazard #1 (port conflicts surface as
    a hard error, not a soft fallback)."""
    rt = LocalRuntime(tmp_path, deployment_config=_valid_yaml_dict())
    fake_docker.containers.get.side_effect = runtime_module.docker.errors.NotFound(
        "no container"
    )

    def _fail_run(cmd: list[str], **kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(runtime_module.subprocess, "run", _fail_run)

    with pytest.raises(subprocess.CalledProcessError):
        rt._start_observability_dev_local()
