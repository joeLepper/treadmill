"""LocalRuntime — orchestrates `up`, `down`, `status`, `logs` for the spike.

The runtime owns:
  - one Docker network (`treadmill-local`)
  - one moto container (`treadmill-local-moto`) on a known port
  - any other Treadmill-managed containers (Postgres, Redis, OTEL, workers — added later)

All managed containers carry the label `treadmill.managed=true` so they are
discoverable for shutdown even after a crash.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import botocore.exceptions
import docker
from rich.console import Console
from rich.table import Table

from treadmill_local.provisioner import MotoProvisioner
from treadmill_local.runner import (
    ContainerSpec,
    LocalNetworkConfig,
    ServiceSpec,
    find_spec,
    resolve_services,
    resolve_task_definitions,
)
from treadmill_local.synth import SynthResult, synth

if TYPE_CHECKING:
    from docker.models.containers import Container

console = Console()

LABEL_KEY = "treadmill.managed"
NETWORK_NAME = "treadmill-local"
MOTO_CONTAINER_NAME = "treadmill-local-moto"
MOTO_IMAGE = "motoserver/moto:5.0.28"
MOTO_HOST_PORT = 5001
MOTO_CONTAINER_PORT = 5000

STATE_DIR = Path(".treadmill-local")
"""Resolved against the process's working directory. The CLI sets cwd to
the repo root before any command runs (``cli.py`` typer callback), so in
production this lands at ``<repo>/.treadmill-local/``. Tests use
``monkeypatch.chdir(tmp_path)`` to sandbox state, which depends on the
relative-path semantics."""

AUTOSCALER_PID_FILE = STATE_DIR / "autoscaler.pid"
AUTOSCALER_LOG_FILE = STATE_DIR / "autoscaler.log"
"""Autoscaler log file. Doubles as the heartbeat substrate — the
autoscaler logs ``tick:`` at INFO on every loop iteration, and
``status`` reads this file's mtime to detect the silent-death failure
mode captured 2026-05-17 (process alive but loop ceased)."""
SCHEDULER_PID_FILE = STATE_DIR / "scheduler.pid"
SCHEDULER_LOG_FILE = STATE_DIR / "scheduler.log"
DEPLOY_WATCHER_PID_FILE = STATE_DIR / "deploy-watcher.pid"
DEPLOY_WATCHER_LOG_FILE = STATE_DIR / "deploy-watcher.log"

# Observability stack (ADR-0043) — five containers managed by docker
# compose, not by a host subprocess. There's no PID file because the
# lifecycle is owned by ``docker compose`` itself; we identify the
# stack by one of its container names (the otel-collector is a stable
# proxy — every service in the compose unit shares its lifecycle).
OBSERVABILITY_COMPOSE_FILE_REL = Path("infra") / "observability" / "docker-compose.yml"
OBSERVABILITY_COMPOSE_LOCAL_FILE_REL = (
    Path("infra") / "observability" / "docker-compose.local.yml"
)
OBSERVABILITY_OTEL_CONTAINER_NAME = "treadmill-otel-collector"
OBSERVABILITY_OTLP_HTTP_URL = "http://localhost:4318"

# Grafana host port substituted into ``docker-compose.local.yml`` via the
# ``GRAFANA_HOST_PORT`` env-var. 3001 (not 3000) sidesteps the common
# port-3000 collision on laptops also running a dashboard / Next.js dev
# server (bunkhouse-dashboard observed 2026-05-19). Operators override
# via ``aws.observability_grafana_port`` in
# ``~/.treadmill/<deployment>.yaml``.
OBSERVABILITY_GRAFANA_HOST_PORT_DEFAULT = 3001

BARE_REPOS_DIR = STATE_DIR / "repos"
"""Host-side directory for the agent worker's local-mode bare repos.
The runtime mounts this into the worker container at
``/var/treadmill/repos`` so the worker can ``git clone file://...``."""

AGENT_FAMILY = "treadmill-agent"
"""Worker family that gets the Claude credentials + bare-repos volume
mounts. Other worker families do not."""

API_FAMILY = "treadmill-api"
"""API service family. Receives the dev-local AWS env wiring."""

POSTGRES_FAMILY = "treadmill-postgres"
REDIS_FAMILY = "treadmill-redis"

# Default images for dev-local services. CDK's CloudLite stack carries
# no ECS task definitions (compute is local per ADR-0016) so the runtime
# defines the container shapes here. ``:dev`` tags are local-only —
# they must be built before ``up`` (matching the fully-local
# convention in ``_ensure_image``).
DEV_LOCAL_POSTGRES_IMAGE = "postgres:16-alpine"
DEV_LOCAL_REDIS_IMAGE = "redis:7-alpine"
DEV_LOCAL_API_IMAGE = "treadmill-api:dev"
DEV_LOCAL_AGENT_IMAGE = "treadmill-agent:dev"

# Container-network DNS hostnames the API + worker reach internal
# services through. The service container name doubles as the DNS name
# on the docker network (per ``_start_service_container``), so these
# constants are the same string referenced in two places.
_API_INTERNAL_DB_URL = (
    "postgresql+asyncpg://postgres:postgres@treadmill-postgres:5432/treadmill"
)
_API_INTERNAL_REDIS_URL = "redis://treadmill-redis:6379/0"

# Host-side Postgres URL for subprocesses that run on the host (not in Docker)
# but need to reach the Postgres container. Port 15432 mirrors the mapping in
# ``_build_dev_local_service_specs`` → ``port_mappings=[(5432, 15432)]``.
_SCHEDULER_HOST_DB_URL = (
    "postgresql+asyncpg://postgres:postgres@localhost:15432/treadmill"
)


def find_repo_root() -> Path:
    """Return the Treadmill repo root.

    The local-adapter is always installed via uv from this repo's workspace,
    so ``Path(__file__)`` reliably sits at
    ``<repo>/tools/local-adapter/treadmill_local/runtime.py`` — four parents
    up is the repo root. We sanity-check by asserting the resulting path
    contains a ``pyproject.toml`` with a ``[tool.uv.workspace]`` table (the
    repo-root marker); if that's missing we fall back to walking up from
    ``cwd`` looking for the same marker, which covers exotic install layouts
    (e.g., a globally-installed wheel running from inside a fresh checkout).
    """
    here = Path(__file__).resolve()
    candidate = here.parents[3]
    if _is_repo_root(candidate):
        return candidate

    cursor = Path.cwd().resolve()
    for directory in [cursor, *cursor.parents]:
        if _is_repo_root(directory):
            return directory
    raise RuntimeError(
        f"could not locate the Treadmill repo root from {here} or {cursor}; "
        "expected a pyproject.toml with [tool.uv.workspace] or a cdk.json."
    )


def _is_repo_root(path: Path) -> bool:
    """Heuristic: a directory is the repo root if it has the workspace
    ``pyproject.toml`` or sits next to ``infra/cdk.json``."""
    pyproject = path / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text()
        except OSError:
            return False
        if "[tool.uv.workspace]" in text:
            return True
    return (path / "infra" / "cdk.json").exists()


_GITHUB_HTTPS_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"
)
_GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$"
)


def parse_github_origin(repo_root: Path) -> tuple[str, str]:
    """Read ``origin`` from ``repo_root``'s git config and return (owner, repo).

    The deploy-watcher subprocess needs ``GITHUB_OWNER`` + ``GITHUB_REPO`` to
    call the GitHub PR-files API (deploy_watcher.py reads them from env at
    startup). Deriving them from the local checkout's git remote keeps the
    operator out of the loop — same value the watcher needs is already
    sitting in ``.git/config``.

    Supports both URL forms:
      * ``https://github.com/<owner>/<repo>(.git)?``
      * ``git@github.com:<owner>/<repo>(.git)?``
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            f"could not read 'origin' remote from {repo_root}: {exc}"
        ) from exc
    url = result.stdout.strip()
    match = _GITHUB_HTTPS_RE.match(url) or _GITHUB_SSH_RE.match(url)
    if match is None:
        raise RuntimeError(
            f"'origin' remote {url!r} does not look like a GitHub URL; "
            "expected https://github.com/<owner>/<repo> or "
            "git@github.com:<owner>/<repo>."
        )
    return match.group(1), match.group(2)


@dataclass
class RuntimeState:
    """In-memory snapshot of what's running."""

    moto_endpoint: str | None = None
    network_id: str | None = None
    refs: dict[str, str] | None = None
    container_specs: list[ContainerSpec] | None = None
    service_specs: list[ServiceSpec] | None = None


class LocalRuntime:
    def __init__(
        self,
        infra_dir: Path,
        *,
        deployment_config: dict[str, Any] | None = None,
        build_images: bool = True,
        start_autoscaler: bool = True,
        start_scheduler: bool = True,
        start_deploy_watcher: bool = True,
        start_observability: bool = True,
    ) -> None:
        """Construct a LocalRuntime.

        Args:
            infra_dir: Path to the CDK app directory (containing
                ``cdk.json``). Ignored when ``deployment_config`` is
                passed — dev-local mode doesn't need ``cdk synth`` at
                ``up`` time (it reads the already-synthed resource URLs
                from the YAML).
            deployment_config: When set, the runtime is in dev-local
                mode. The dict is the parsed contents of
                ``~/.treadmill/<deployment_id>.yaml`` per ADR-0016.
                Moto is skipped (real AWS is the substrate); Postgres
                + Redis + API + agent containers are started with env
                drawn from this dict. When ``None`` (default), the
                fully-local + moto path runs unchanged.
            build_images: When True (default), ``up`` and
                ``start_worker_once`` rebuild ``treadmill-api:dev`` and
                ``treadmill-agent:dev`` from current source before
                launching containers — Docker's layer cache makes this
                a sub-second no-op when nothing changed and prevents
                the silent "stale image" failure mode. Set to False
                (via ``--no-build``) when the operator deliberately
                wants to use an already-built image (e.g., debugging
                with a known-good build).
            start_autoscaler: When True (default), ``up`` spawns the
                autoscaler subprocess after services are up (per
                ADR-0018). Set to False (via ``--no-autoscaler``) to
                run a stack without on-demand worker spawning — useful
                for debugging a specific worker failure in isolation
                with manual ``run-worker`` control.
            start_scheduler: When True (default), dev-local ``up``
                spawns the scheduler subprocess after services are up.
                Set to False (via ``--no-scheduler``) to suppress the
                cron-dispatch loop — useful when testing schedule-free
                workflows. Ignored in fully-local (moto) mode: the
                scheduler needs a real Postgres schema and runs
                against it on the host.
            start_deploy_watcher: When True (default), dev-local ``up``
                spawns the deploy-watcher subprocess after services are
                up. Set to False (via ``--no-deploy-watcher``) to
                suppress the deploy-event reconciliation loop — useful
                when debugging without automated container updates on
                merged PRs. Ignored in fully-local (moto) mode.
            start_observability: When True (default), dev-local ``up``
                brings up the observability compose stack (OTel
                Collector + Loki + Prometheus + Tempo + Grafana) via
                ``docker compose -f infra/observability/docker-compose.yml
                -f infra/observability/docker-compose.local.yml up -d``
                (per ADR-0043). Set to False (via ``--no-observability``)
                when running without the stack — useful for debugging
                without observability, or when laptop memory matters
                more than dashboards. Ignored in fully-local (moto)
                mode: ADR-0020 keeps fully-local OTLP-free.
        """
        self.infra_dir = infra_dir.resolve()
        self.docker = docker.from_env()
        self.state = RuntimeState()
        self.deployment_config = deployment_config
        self.build_images = build_images
        self.start_autoscaler = start_autoscaler
        self.start_scheduler = start_scheduler
        self.start_deploy_watcher = start_deploy_watcher
        self.start_observability = start_observability
        # Per ADR-0019: dev-local credentials are fetched on the host and
        # injected into containers as env vars. The fetched values live in
        # memory on the runtime for the lifetime of the up-process; we
        # never write them to disk. All attrs are populated lazily by
        # ``_ensure_dev_local_credentials`` the first time we need to
        # build container env (``up`` or ``start_worker_once``).
        self._worker_aws_env: dict[str, str] | None = None
        self._api_aws_env: dict[str, str] | None = None
        self._github_token: str | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def up(self) -> None:
        if self.deployment_config is not None:
            self._up_dev_local()
            return
        console.print("[bold]Treadmill local — up[/bold]")
        self._ensure_network()
        self._ensure_images_built()
        self._start_moto()
        self._wait_for_moto()
        self._ensure_provisioned()
        self._start_services()
        if self.start_autoscaler:
            self._start_autoscaler()
        else:
            console.print(
                "[yellow]• Autoscaler suppressed (--no-autoscaler).[/yellow]"
            )
        self._report_up()

    # ── Dev-local mode (ADR-0016) ─────────────────────────────────────────────

    def _up_dev_local(self) -> None:
        """Bring up Postgres + Redis + API in dev-local mode.

        Skips moto (the substrate is real AWS) and skips ``cdk synth``
        (the resource URLs/ARNs come from ``~/.treadmill/<id>.yaml``,
        produced by C.4's ``init`` subcommand).

        Per ADR-0019, the AWS credentials for both the worker and API are
        long-lived IAM-User keys fetched from Secrets Manager on the host
        and injected as env vars before any container starts. The values
        live in memory on this runtime and are injected as env vars on
        every container we spawn — no ``~/.aws`` mount, no SSO inside
        containers.

        The agent worker is NOT started here — it's launched on-demand
        by ``start_worker_once`` (same pattern as fully-local mode),
        so this method only stands up the long-running services.
        """
        assert self.deployment_config is not None
        cfg = self.deployment_config
        deployment_id = cfg["deployment_id"]
        console.print(
            f"[bold]Treadmill local — up (dev-local, deployment={deployment_id})[/bold]"
        )
        self._ensure_network()
        self._ensure_images_built()
        # Fetch creds on the host before building specs — the env on each
        # spec needs them. Fail-fast on SSO-expired with a clear message.
        self._ensure_dev_local_credentials()
        specs = self._build_dev_local_service_specs(cfg)
        self.state.service_specs = specs
        # Build the agent ContainerSpec too so ``start_worker_once`` can
        # find it without re-synthing CDK (dev-local has no CDK compute).
        self.state.container_specs = [
            self._build_dev_local_agent_spec(cfg),
        ]
        self._start_services()
        if self.start_autoscaler:
            self._start_autoscaler_dev_local()
        else:
            console.print(
                "[yellow]• Autoscaler suppressed (--no-autoscaler).[/yellow]"
            )
        if self.start_scheduler:
            self._start_scheduler_dev_local()
        else:
            console.print(
                "[yellow]• Scheduler suppressed (--no-scheduler).[/yellow]"
            )
        # Observability ahead of the deploy-watcher so the watcher's
        # OTLP exporter has a live collector to publish to on startup
        # (per ADR-0043).
        if self.start_observability:
            self._start_observability_dev_local()
        else:
            console.print(
                "[yellow]• Observability suppressed (--no-observability).[/yellow]"
            )
        if self.start_deploy_watcher:
            self._start_deploy_watcher_dev_local()
        else:
            console.print(
                "[yellow]• Deploy watcher suppressed (--no-deploy-watcher).[/yellow]"
            )
        self._report_up_dev_local(cfg)

    def _ensure_dev_local_credentials(self) -> None:
        """Populate ``self._worker_aws_env`` + ``self._api_aws_env``.

        Idempotent — all attributes are only fetched once per runtime
        instance lifetime. Called from ``_up_dev_local`` (initial fetch)
        and ``start_worker_once`` (when the worker is launched from a
        fresh CLI process whose ``LocalRuntime`` has no in-memory state).
        """
        assert self.deployment_config is not None
        cfg = self.deployment_config
        if self._api_aws_env is None:
            self._api_aws_env = self._fetch_api_credentials(cfg)
        if self._worker_aws_env is None:
            self._worker_aws_env = self._fetch_worker_credentials(cfg)
        # ADR-0049: skip the personal-PAT fetch once the GitHub App is
        # configured — components mint installation tokens instead, and the
        # PAT secret may be deleted (phase-8 decommission).
        _app_mode = bool(
            cfg.get("github_app_id")
            and cfg.get("secrets", {}).get("github_app_private_key_secret_name")
        )
        if self._github_token is None and not _app_mode:
            self._github_token = self._fetch_github_pat(cfg)

    @staticmethod
    def _fetch_api_credentials(cfg: dict[str, Any]) -> dict[str, str]:
        """Fetch the API's IAM-User keys from Secrets Manager.

        Per ADR-0019: the local-adapter resolves the API credentials
        once on the host (using the operator's profile) and injects
        them as ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` env
        vars on the API container. The API's boto3 reads them
        via the standard env-var credential resolution — no Secrets
        Manager call at startup, no SSO inside the container.

        The secret name comes from the deployment YAML's
        ``secrets.api_aws_credentials_secret_name`` (populated by
        ``treadmill-local init`` from the ``ApiAwsCredentialsSecretName``
        CFN output). The secret payload is JSON of shape
        ``{"aws_access_key_id": "...", "aws_secret_access_key": "..."}``.
        """
        profile = cfg["aws_profile"]
        region = cfg["aws_region"]
        secret_name = cfg["secrets"]["api_aws_credentials_secret_name"]

        session = boto3.Session(profile_name=profile, region_name=region)
        secrets = session.client("secretsmanager")
        try:
            resp = secrets.get_secret_value(SecretId=secret_name)
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"ExpiredToken", "ExpiredTokenException"}:
                console.print(
                    f"[red]AWS credentials for profile {profile!r} have expired "
                    f"mid-fetch of {secret_name!r}.[/red]"
                )
                console.print(
                    f"[red]→ Run `aws sso login --profile {profile}` and re-run "
                    f"`treadmill-local up`.[/red]"
                )
                raise SystemExit(1) from exc
            raise

        raw = resp.get("SecretString")
        if not raw:
            raise RuntimeError(
                f"API AWS credentials secret {secret_name!r} has no SecretString; "
                f"run `aws iam create-access-key --user-name <api-user> && "
                f"aws secretsmanager put-secret-value --secret-id {secret_name!r} "
                f"--secret-string '{{\"aws_access_key_id\": \"...\", "
                f"\"aws_secret_access_key\": \"...\"}}'` and re-try."
            )
        creds = json.loads(raw)
        access_key = creds.get("aws_access_key_id")
        secret_key = creds.get("aws_secret_access_key")
        if not access_key or not secret_key:
            raise RuntimeError(
                f"API AWS credentials secret {secret_name!r} is missing "
                f"aws_access_key_id / aws_secret_access_key"
            )
        return {
            "AWS_ACCESS_KEY_ID": access_key,
            "AWS_SECRET_ACCESS_KEY": secret_key,
        }

    @staticmethod
    def _fetch_worker_credentials(cfg: dict[str, Any]) -> dict[str, str]:
        """Fetch the worker's IAM-User keys from Secrets Manager.

        Per ADR-0019: the local-adapter resolves the worker credentials
        once on the host (using the operator's SSO profile) and injects
        them as ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` env
        vars on every worker container. The worker's boto3 reads them
        via the standard env-var credential resolution — no Secrets
        Manager call at worker startup, no SSO inside the container.

        The secret name comes from the deployment YAML's
        ``secrets.worker_aws_credentials_secret_name`` (populated by
        ``treadmill-local init`` from the ``WorkerAwsCredentialsSecretName``
        CFN output). The secret payload is JSON of shape
        ``{"aws_access_key_id": "...", "aws_secret_access_key": "..."}``.
        """
        profile = cfg["aws_profile"]
        region = cfg["aws_region"]
        secret_name = cfg["secrets"]["worker_aws_credentials_secret_name"]

        session = boto3.Session(profile_name=profile, region_name=region)
        secrets = session.client("secretsmanager")
        try:
            resp = secrets.get_secret_value(SecretId=secret_name)
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"ExpiredToken", "ExpiredTokenException"}:
                console.print(
                    f"[red]AWS credentials for profile {profile!r} have expired "
                    f"mid-fetch of {secret_name!r}.[/red]"
                )
                console.print(
                    f"[red]→ Run `aws sso login --profile {profile}` and re-run "
                    f"`treadmill-local up`.[/red]"
                )
                raise SystemExit(1) from exc
            raise

        raw = resp.get("SecretString")
        if not raw:
            raise RuntimeError(
                f"worker AWS credentials secret {secret_name!r} has no SecretString"
            )
        creds = json.loads(raw)
        access_key = creds.get("aws_access_key_id")
        secret_key = creds.get("aws_secret_access_key")
        if not access_key or not secret_key:
            raise RuntimeError(
                f"worker AWS credentials secret {secret_name!r} is missing "
                f"aws_access_key_id / aws_secret_access_key"
            )
        return {
            "AWS_ACCESS_KEY_ID": access_key,
            "AWS_SECRET_ACCESS_KEY": secret_key,
        }

    @staticmethod
    def _fetch_github_pat(cfg: dict[str, Any]) -> str:
        """Fetch the GitHub PAT from Secrets Manager.

        The API container needs ``GITHUB_TOKEN`` set so the
        ``github_client`` httpx.AsyncClient is constructed at startup
        (per ``treadmill_api.app``). Without it, ADR-0021's plan-doc
        trigger silently no-ops because it can't fetch plan-doc content
        from GitHub. Same host-side-fetch pattern as the worker IAM
        keys (ADR-0019).
        """
        profile = cfg["aws_profile"]
        region = cfg["aws_region"]
        secret_name = cfg["secrets"]["github_pat_secret_name"]

        session = boto3.Session(profile_name=profile, region_name=region)
        secrets = session.client("secretsmanager")
        try:
            resp = secrets.get_secret_value(SecretId=secret_name)
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"ExpiredToken", "ExpiredTokenException"}:
                console.print(
                    f"[red]AWS credentials for profile {profile!r} have expired "
                    f"mid-fetch of {secret_name!r}.[/red]"
                )
                console.print(
                    f"[red]→ Run `aws sso login --profile {profile}` and re-run "
                    f"`treadmill-local up`.[/red]"
                )
                raise SystemExit(1) from exc
            raise

        pat = resp.get("SecretString")
        if not pat:
            raise RuntimeError(
                f"GitHub PAT secret {secret_name!r} has no SecretString"
            )
        return pat.strip()

    @staticmethod
    def _fetch_secret_string(cfg: dict[str, Any], secret_name: str) -> str:
        """Fetch a secret's ``SecretString`` from Secrets Manager.

        Generalizes ``_fetch_github_pat`` for ADR-0049's GitHub App private
        key (and any future host-side secret), with the same expired-SSO
        handling.
        """
        profile = cfg["aws_profile"]
        region = cfg["aws_region"]
        session = boto3.Session(profile_name=profile, region_name=region)
        secrets = session.client("secretsmanager")
        try:
            resp = secrets.get_secret_value(SecretId=secret_name)
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"ExpiredToken", "ExpiredTokenException"}:
                console.print(
                    f"[red]AWS credentials for profile {profile!r} have expired "
                    f"mid-fetch of {secret_name!r}.[/red]"
                )
                console.print(
                    f"[red]→ Run `aws sso login --profile {profile}` and re-run "
                    f"`treadmill-local up`.[/red]"
                )
                raise SystemExit(1) from exc
            raise
        value = resp.get("SecretString")
        if not value:
            raise RuntimeError(f"secret {secret_name!r} has no SecretString")
        return value

    def _build_dev_local_service_specs(
        self,
        cfg: dict[str, Any],
    ) -> list[ServiceSpec]:
        """Return ServiceSpec list for Postgres + Redis + API in dev-local.

        These specs are constructed in-process (not from CDK) because
        ``TreadmillCloudLite`` has no compute resources — ADR-0016
        keeps compute on the laptop. The shape mirrors what the legacy
        ``TreadmillSpike`` template synthesized for fully-local mode so
        existing service-name + DNS expectations carry over.
        """
        return [
            ServiceSpec(
                family=POSTGRES_FAMILY,
                desired_count=1,
                container_specs=[
                    ContainerSpec(
                        family=POSTGRES_FAMILY,
                        name=POSTGRES_FAMILY,
                        image=DEV_LOCAL_POSTGRES_IMAGE,
                        env={
                            "POSTGRES_DB": "treadmill",
                            "POSTGRES_USER": "postgres",
                            "POSTGRES_PASSWORD": "postgres",
                        },
                        network=NETWORK_NAME,
                        container_ports=[5432],
                    ),
                ],
                port_mappings=[(5432, 15432)],
            ),
            ServiceSpec(
                family=REDIS_FAMILY,
                desired_count=1,
                container_specs=[
                    ContainerSpec(
                        family=REDIS_FAMILY,
                        name=REDIS_FAMILY,
                        image=DEV_LOCAL_REDIS_IMAGE,
                        env={},
                        network=NETWORK_NAME,
                        container_ports=[6379],
                    ),
                ],
                port_mappings=[(6379, 16379)],
            ),
            ServiceSpec(
                family=API_FAMILY,
                desired_count=1,
                container_specs=[
                    ContainerSpec(
                        family=API_FAMILY,
                        name=API_FAMILY,
                        image=DEV_LOCAL_API_IMAGE,
                        env=self._dev_local_api_env(cfg),
                        network=NETWORK_NAME,
                        container_ports=[8088],
                    ),
                ],
                port_mappings=[(8088, 8088)],
            ),
        ]

    def _build_dev_local_agent_spec(
        self,
        cfg: dict[str, Any],
    ) -> ContainerSpec:
        """Return the ContainerSpec the autoscaler / ``run-worker``
        uses to launch agent worker containers in dev-local mode."""
        return ContainerSpec(
            family=AGENT_FAMILY,
            name=AGENT_FAMILY,
            image=DEV_LOCAL_AGENT_IMAGE,
            env=self._dev_local_worker_env(cfg),
            network=NETWORK_NAME,
            container_ports=[],
        )

    def _dev_local_api_env(self, cfg: dict[str, Any]) -> dict[str, str]:
        """Build the API container's env from the deployment YAML.

        Env-var spellings match ``services/api/treadmill_api/config.py``:

        - ``TREADMILL_*`` prefixed where ``Settings`` reads via the
          ``env_prefix="TREADMILL_"`` (e.g., ``TREADMILL_DEPLOYMENT_MODE``)
        - Unprefixed where the ``Settings`` field has an explicit
          ``alias`` (e.g., ``EVENTS_TOPIC_ARN``, ``WORK_QUEUE_URL``,
          ``DATABASE_URL``, ``GITHUB_WEBHOOK_SECRET_NAME``)

        Notably absent: ``AWS_ENDPOINT_URL``. That's the moto override;
        dev-local talks to the real AWS endpoint via the standard boto3
        resolver.

        Per ADR-0019: ``AWS_PROFILE`` is **not** set; instead the
        API's IAM-User keys are exported as ``AWS_ACCESS_KEY_ID`` /
        ``AWS_SECRET_ACCESS_KEY`` env vars on the host before the
        container starts. Boto3's env-var credential resolution picks
        them up and never touches a profile inside the container.
        """
        aws = cfg["aws"]
        secrets = cfg["secrets"]
        # The injected creds are required — _ensure_dev_local_credentials
        # populates them before any spec-build call site.
        self._ensure_dev_local_credentials()
        assert self._api_aws_env is not None
        env: dict[str, str] = {
            # Deployment-mode literal (read by ``Settings.deployment_mode``).
            "TREADMILL_DEPLOYMENT_MODE": "dev_local",
            # AWS routing for boto3 (real AWS endpoint, no moto override).
            "AWS_DEFAULT_REGION": cfg["aws_region"],
            "AWS_REGION": cfg["aws_region"],
            "AWS_ACCOUNT_ID": cfg["aws_account_id"],
            # AWS resource ARNs/URLs (aliased fields, no TREADMILL_ prefix).
            "EVENTS_TOPIC_ARN": aws["events_topic_arn"],
            "EVENTS_QUEUE_URL": aws["events_queue_url"],
            "WORK_QUEUE_URL": aws["work_queue_url"],
            "WEBHOOK_INBOX_QUEUE_URL": aws["webhook_inbox_queue_url"],
            # Webhook secret name in Secrets Manager (ADR-0017 path).
            "GITHUB_WEBHOOK_SECRET_NAME": secrets["github_webhook_secret_name"],
            # Local-side wiring — Postgres + Redis run as sibling
            # containers on the docker network and are reachable by
            # service name. We deliberately ignore ``cfg['local']['*']``
            # (those are host-side spellings for the operator's own
            # ``psql`` / ``redis-cli``) and use the container-DNS form
            # here.
            "DATABASE_URL": _API_INTERNAL_DB_URL,
            "REDIS_URL": _API_INTERNAL_REDIS_URL,
        }
        # Inject the API's IAM-User keys last so the
        # env-var dict carries the credential keys the container's
        # boto3 will pick up via the standard env-var resolver.
        env.update(self._api_aws_env)
        # GITHUB_TOKEN: the API constructs an httpx.AsyncClient against
        # the GitHub API for ADR-0013's conflict-detection sweep AND
        # ADR-0021's plan-doc trigger handler. Without it the handler
        # silently no-ops on pr_merged events. The token comes from the
        # same Secrets Manager entry the worker uses for git operations.
        # ADR-0049: inject the legacy PAT only when the App is not configured.
        # With the App active the API mints per-repo installation tokens
        # (GITHUB_APP_ID + key injected above) and needs no PAT.
        if not (
            cfg.get("github_app_id")
            and cfg.get("secrets", {}).get("github_app_private_key_secret_name")
        ):
            assert self._github_token is not None
            env["GITHUB_TOKEN"] = self._github_token
        # ADR-0049: GitHub App identity. When the deployment configures an
        # App id + private-key secret, inject them so the API mints per-repo
        # installation tokens (``build_github_clients`` selects the App path);
        # absent → the API stays on the PAT above. The App webhook-secret name
        # is injected too for the dual-secret webhook verifier (phase 6).
        app_id = cfg.get("github_app_id")
        secrets_cfg = cfg.get("secrets", {})
        app_key_secret = secrets_cfg.get("github_app_private_key_secret_name")
        if app_id and app_key_secret:
            env["GITHUB_APP_ID"] = str(app_id)
            env["GITHUB_APP_PRIVATE_KEY"] = self._fetch_secret_string(
                cfg, app_key_secret,
            )
            # Inject the App webhook secret VALUE (host-side fetch via the
            # operator's creds), not the name — the API's IAM user has no
            # GetSecretValue on this manually-created secret, so the poller
            # can't fetch it itself (ADR-0049 phase 6).
            app_webhook_secret_name = secrets_cfg.get(
                "github_app_webhook_secret_name"
            )
            if app_webhook_secret_name:
                env["GITHUB_APP_WEBHOOK_SECRET"] = self._fetch_secret_string(
                    cfg, app_webhook_secret_name,
                )
        # ADR-0020: inject OTLP endpoint when the observability stack is
        # deployed. The OTel SDK no-ops silently when the var is unset
        # (fully-local mode). Value from the deployment YAML under
        # aws.observability_collector_endpoint (written by treadmill-local
        # init from the ObservabilityCollectorEndpoint CFN output).
        collector = cfg.get("aws", {}).get("observability_collector_endpoint")
        if collector:
            # Accept value with or without scheme. Operator yaml may carry
            # ``127.0.0.1:4318`` (bare host:port, from a CFN output) or
            # ``http://127.0.0.1:4318`` (full URL). Either is canonical;
            # we don't want ``http://http://...`` when the value already
            # carries the scheme.
            if not collector.startswith(("http://", "https://")):
                collector = f"http://{collector}"
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = collector
            # The OTel Python SDK defaults its exporter to gRPC (port 4317).
            # The collector listens on 4318 for OTLP HTTP and 4317 for gRPC,
            # and the endpoint above names the HTTP port (consistent with
            # the ``http://`` scheme and the host-side port mapping in
            # ``infra/observability/docker-compose.yml``). Force HTTP/protobuf
            # so the SDK's transport matches the port. The unsuffixed
            # ``OTEL_EXPORTER_OTLP_PROTOCOL`` is the catch-all that applies
            # to all signals (traces + metrics + logs) when the
            # signal-specific spellings are absent.
            env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        return env

    def _dev_local_worker_env(self, cfg: dict[str, Any]) -> dict[str, str]:
        """Build the agent worker's env from the deployment YAML.

        These spellings reflect ``workers/agent/treadmill_agent/config.py``
        plus the github-mode contract from D.1 + D.3.

        Per ADR-0019: the worker no longer fetches its own AWS keys.
        The local-adapter resolves the long-lived IAM-User keys from
        Secrets Manager on the host and injects them here as
        ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``. The
        worker's boto3 reads them via the standard env-var chain;
        ``WORKER_AWS_CREDENTIALS_SECRET_NAME`` is no longer passed to
        the container (the worker has no need for it).
        """
        aws = cfg["aws"]
        secrets = cfg["secrets"]
        self._ensure_dev_local_credentials()
        assert self._worker_aws_env is not None
        env: dict[str, str] = {
            # Treadmill-specific (no env prefix on the worker side; it
            # reads via direct ``os.environ.get`` rather than pydantic).
            "REPO_MODE": "github",
            "WORK_QUEUE_URL": aws["work_queue_url"],
            "EVENTS_TOPIC_ARN": aws["events_topic_arn"],
            "TREADMILL_API_URL": "http://treadmill-api:8088",
            # github-mode auth — the worker still fetches its own PAT
            # at startup using the injected AWS credentials below.
            "GITHUB_PAT_SECRET_NAME": secrets["github_pat_secret_name"],
            # AWS routing — real AWS, no moto override. No AWS_PROFILE
            # (per ADR-0019: env-var creds win over profile-based).
            "AWS_DEFAULT_REGION": cfg["aws_region"],
            "AWS_REGION": cfg["aws_region"],
        }
        # Worker IAM-User keys, fetched once on the host (see
        # ``_fetch_worker_credentials``) and injected here.
        env.update(self._worker_aws_env)
        # ADR-0049: when the deployment configures a GitHub App, the worker
        # mints a short-lived installation token from the API at startup
        # (GITHUB_AUTH_MODE=app) instead of fetching the personal PAT. The
        # PAT secret name stays set as a fallback until phase-8 decommission.
        if cfg.get("github_app_id"):
            env["GITHUB_AUTH_MODE"] = "app"
        # ADR-0020: inject OTLP endpoint when the observability stack is
        # deployed. No-ops when unset (fully-local or obs stack absent).
        collector = cfg.get("aws", {}).get("observability_collector_endpoint")
        if collector:
            # Accept value with or without scheme. Operator yaml may carry
            # ``127.0.0.1:4318`` (bare host:port, from a CFN output) or
            # ``http://127.0.0.1:4318`` (full URL). Either is canonical;
            # we don't want ``http://http://...`` when the value already
            # carries the scheme.
            if not collector.startswith(("http://", "https://")):
                collector = f"http://{collector}"
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = collector
            # See ``_dev_local_api_env`` for rationale — the OTel Python
            # SDK defaults to gRPC (port 4317) but the endpoint above
            # names the HTTP port (4318). Force HTTP/protobuf so transport
            # matches the port. The unsuffixed catch-all applies to all
            # signals (traces + metrics + logs).
            env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        return env

    def _report_up_dev_local(self, cfg: dict[str, Any]) -> None:
        console.rule("[bold green]Treadmill local — ready (dev-local)[/bold green]")
        console.print(f"  deployment:        [cyan]{cfg['deployment_id']}[/cyan]")
        console.print(f"  aws_profile:       [cyan]{cfg['aws_profile']}[/cyan]")
        console.print(f"  aws_region:        [cyan]{cfg['aws_region']}[/cyan]")
        console.print(
            f"  aws_account_id:    [cyan]{cfg['aws_account_id']}[/cyan]"
        )
        console.print(
            f"  api:               [cyan]http://localhost:8088[/cyan]"
        )
        console.print(
            f"  webhook_inbox:     [dim]{cfg['aws']['webhook_inbox_queue_url']}[/dim]"
        )
        console.print(
            f"  work_queue:        [dim]{cfg['aws']['work_queue_url']}[/dim]"
        )

    def _ensure_provisioned(self) -> None:
        """Synth + provision into moto (idempotent). Populates refs and specs.

        Used by both ``up()`` (initial provisioning) and ``start_worker_once()``
        (in case the worker is started from a separate CLI invocation where
        the runtime instance lost its in-memory state).
        """
        if self.state.moto_endpoint is None:
            self._discover_running_moto()
        if self.state.moto_endpoint is None:
            raise RuntimeError(
                "moto is not running; bring the runtime up first with `treadmill-local up`."
            )
        result = self._synth()
        self._provision(result)

    def _discover_running_moto(self) -> None:
        """If moto is already running from a prior `up`, populate the endpoint."""
        try:
            container = self.docker.containers.get(MOTO_CONTAINER_NAME)
            if container.status == "running":
                self.state.moto_endpoint = f"http://localhost:{MOTO_HOST_PORT}"
        except docker.errors.NotFound:
            pass

    def down(self) -> None:
        console.print("[bold]Treadmill local — down[/bold]")
        self._stop_autoscaler()
        self._stop_scheduler()
        self._stop_deploy_watcher()
        self._stop_observability()
        self._stop_managed_containers()
        self._remove_network()
        console.print("[green]Down complete.[/green]")

    def redeploy(self, *, skip_cdk: bool = False) -> None:
        """End-to-end redeploy: cdk deploy → down → up.

        Dev-local only — fully-local has no AWS to redeploy. The
        caller (CLI) guards this; this method asserts.

        Fail-fast: any step's failure short-circuits the rest so the
        operator can investigate without a half-cycled stack. The
        ``cdk deploy`` step is idempotent — passing it through every
        redeploy costs ~a few seconds of synth + a no-op CFN check
        if nothing changed.
        """
        assert self.deployment_config is not None, (
            "redeploy requires a deployment config (dev-local only)"
        )
        cfg = self.deployment_config
        deployment_id = cfg["deployment_id"]
        console.print(
            f"[bold]Treadmill local — redeploy "
            f"(deployment={deployment_id})[/bold]"
        )

        if not skip_cdk:
            self._cdk_deploy(cfg)
        else:
            console.print(
                "[dim]• --no-cdk: skipping cdk deploy[/dim]"
            )

        # down before up so the running stack picks up new images +
        # any container env from the freshly-synthed CDK outputs.
        # ``down`` is idempotent — safe even if the stack was already
        # stopped (e.g., operator running redeploy from a clean state).
        self.down()
        self.up()
        console.rule(
            f"[bold green]Treadmill local — redeploy complete "
            f"(deployment={deployment_id})[/bold green]"
        )

    def _cdk_deploy(self, cfg: dict[str, Any]) -> None:
        """Shell out to ``cdk deploy`` for the dev-local stack.

        Inherits the operator's env (AWS_PROFILE comes from the
        deployment YAML, prepended onto the parent env). The
        ``--require-approval never`` flag bypasses the interactive
        prompt — appropriate for an operator-initiated redeploy
        (the operator already implicitly approved by running this
        command).
        """
        deployment_id = cfg["deployment_id"]
        profile = cfg["aws_profile"]
        region = cfg["aws_region"]
        stack_name = f"Treadmill{deployment_id.title().replace('_', '')}CloudLite"

        repo_root = find_repo_root()
        infra_dir = repo_root / "infra"

        console.print(
            f"[dim]• Running cdk deploy {stack_name} "
            f"(profile={profile}, region={region})...[/dim]"
        )
        env = {
            **os.environ,
            "AWS_PROFILE": profile,
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
            "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        }
        cmd = [
            "cdk", "deploy", stack_name,
            "--context", "mode=dev_local",
            "--context", f"deployment_id={deployment_id}",
            "--profile", profile,
            "--require-approval", "never",
        ]
        try:
            subprocess.run(
                cmd, cwd=str(infra_dir), env=env, check=True,
            )
        except subprocess.CalledProcessError as exc:
            console.print(
                f"[red]cdk deploy failed (exit {exc.returncode}). "
                f"Aborting redeploy; stack not cycled.[/red]"
            )
            console.print(
                "[red]→ Investigate the cdk output above. Common "
                "causes: expired SSO token (run "
                f"`aws sso login --profile {profile}`); CFN drift "
                "(check the AWS Console); missing context vars.[/red]"
            )
            raise SystemExit(1) from exc
        console.print(
            f"[green]• cdk deploy {stack_name} complete[/green]"
        )

    def status(self) -> None:
        containers = self._managed_containers()
        autoscaler_alive = self._autoscaler_pid_alive()
        scheduler_alive = self._scheduler_pid_alive()

        if not containers and not autoscaler_alive and not scheduler_alive:
            console.print("[yellow]No Treadmill-managed containers running.[/yellow]")
            return

        table = Table(title="Treadmill local — status")
        table.add_column("Name")
        table.add_column("Image / role")
        table.add_column("Status")
        table.add_column("Ports / extra")
        for c in containers:
            ports = ", ".join(
                f"{host['HostPort']}->{port}"
                for port, hosts in (c.attrs["NetworkSettings"]["Ports"] or {}).items()
                for host in (hosts or [])
            ) or "-"
            # A container whose backing image has been removed (image-id
            # 404) raises ``ImageNotFound`` on ``c.image``. Don't crash
            # the whole status output for one stale container; degrade
            # to the image id from the container's attrs.
            try:
                image_label = c.image.tags[0] if c.image.tags else c.image.short_id
            except docker.errors.ImageNotFound:
                # ``Image`` field on the container is the original
                # reference (tag or sha). Fall back to that.
                image_label = (
                    c.attrs.get("Config", {}).get("Image")
                    or "[red]image removed[/red]"
                )
            table.add_row(
                c.name,
                image_label,
                c.status,
                ports,
            )
        if autoscaler_alive:
            pid = AUTOSCALER_PID_FILE.read_text().strip()
            pulse_status = self._autoscaler_pulse_status()
            table.add_row(
                "autoscaler (subprocess)",
                "[role=autoscaler]",
                pulse_status,
                f"pid={pid}, log={AUTOSCALER_LOG_FILE}",
            )
        if scheduler_alive:
            pid = SCHEDULER_PID_FILE.read_text().strip()
            table.add_row(
                "scheduler (subprocess)",
                "[role=scheduler]",
                "running",
                f"pid={pid}, log={SCHEDULER_LOG_FILE}",
            )
        console.print(table)

    def start_worker_once(self, family: str) -> Container:
        """Start one container for the given task family, attached to the
        Treadmill local network and wired with the resolved env. Returns the
        running container handle.

        Idempotently ensures the runtime is provisioned before launching, so
        this works whether called from the same process as up() or from a
        fresh CLI invocation.

        In dev-local mode the container specs are built directly from the
        deployment YAML (no moto / no synth needed), so the
        ``_ensure_provisioned`` fall-back is skipped — that path would
        require moto to be running, which is wrong for dev-local.

        The worker image is rebuilt here too (subject to ``build_images``)
        so an operator running ``run-worker`` against an already-up stack
        picks up worker-code changes made mid-session. Docker's layer
        cache keeps this near-free when nothing changed.
        """
        self._ensure_images_built()
        if self.state.container_specs is None:
            if self.deployment_config is not None:
                # Fresh CLI process (run-worker) — fetch creds on the
                # host before building the agent spec. ``up`` callers
                # have already fetched them; this is idempotent.
                self._ensure_dev_local_credentials()
                self.state.container_specs = [
                    self._build_dev_local_agent_spec(self.deployment_config),
                ]
            else:
                self._ensure_provisioned()
        assert self.state.container_specs is not None
        spec = find_spec(self.state.container_specs, family)
        nonce = int(time.time() * 1000) % 100000
        name = f"treadmill-worker-{spec.family}-{nonce:05d}"
        return self._run_container(spec, name=name, role="worker")

    def _run_container(
        self,
        spec: ContainerSpec,
        *,
        name: str,
        role: str,
        port_mappings: list[tuple[int, int]] | None = None,
    ) -> Container:
        """Start a single container for *spec*. ``role`` becomes the
        ``treadmill.role`` label; ``port_mappings`` (container_port, host_port)
        publishes ports on the host."""
        self._ensure_image(spec.image)
        ports = {f"{cp}/tcp": hp for cp, hp in (port_mappings or [])}
        volumes = self._volumes_for(spec)
        c = self.docker.containers.run(
            spec.image,
            name=name,
            detach=True,
            network=spec.network,
            environment=spec.env,
            ports=ports,
            volumes=volumes,
            labels={
                LABEL_KEY: "true",
                "treadmill.role": role,
                "treadmill.family": spec.family,
            },
            remove=False,
        )
        suffix = f" ports={ports}" if ports else ""
        console.print(f"• {role.capitalize()} [cyan]{name}[/cyan] started ({c.short_id}){suffix}.")
        return c

    def _volumes_for(self, spec: ContainerSpec) -> dict[str, dict[str, str]]:
        """Return the docker-py ``volumes`` mapping for *spec*.

        Agent worker family gets:
          * Claude OAuth credentials (read-write) so Claude Code can
            refresh the host user's OAuth token in place. The mount is
            RW because the CLI rewrites ``.credentials.json`` when it
            refreshes the access token; ``ro`` causes silent auth
            failures once the token expires. Single-worker v0 is safe;
            multi-worker concurrency story is a future ADR (captured in
            the closure plan's risks section).
          * Local bare-repos directory (read-write) so ``REPO_MODE=local``
            can ``git clone file://...`` and push back.

        Per ADR-0019: dev-local mode does **not** mount ``~/.aws`` into
        any container. The operator's SSO session (for the API) and the
        worker's long-lived IAM-User keys are both resolved on the host
        and injected as env vars — boto3's env-var credential resolution
        picks them up inside the container. Mounting ``~/.aws`` was the
        root cause of the SSO-cache-refresh-writeback class of bugs that
        ADR-0019 retires.

        Postgres ships with a **named** volume so DB state persists
        across ``down`` + ``up`` cycles (operator framing 2026-05-14:
        smokes need to be resumable; losing the workflow_runs /
        events / tasks tables every redeploy means every SSO-TTL hit
        is a hard reset). Volume name is deployment-scoped:
        ``treadmill-<deployment_id>-postgres-data`` for dev-local;
        ``treadmill-local-postgres-data`` for fully-local. Docker
        creates the volume automatically if it doesn't exist. To
        explicitly wipe the DB, ``docker volume rm <name>``.

        Redis still ships without a volume — its state is
        regenerable cache (per ADR-0011's "Redis is cache, Postgres
        is source-of-truth"), no harm in losing it on a cycle.
        """
        mounts: dict[str, dict[str, str]] = {}

        # Agent-only mounts (apply in both fully-local + dev-local).
        if spec.family == AGENT_FAMILY:
            creds = Path.home() / ".claude" / ".credentials.json"
            if creds.exists():
                mounts[str(creds)] = {
                    "bind": "/root/.claude/.credentials.json", "mode": "rw",
                }
            bare_root = (Path.cwd() / BARE_REPOS_DIR).resolve()
            bare_root.mkdir(parents=True, exist_ok=True)
            mounts[str(bare_root)] = {
                "bind": "/var/treadmill/repos", "mode": "rw",
            }

        # Postgres: named volume for state persistence across cycles.
        if spec.family == POSTGRES_FAMILY:
            deployment_id = (
                self.deployment_config["deployment_id"]
                if self.deployment_config is not None
                else "local"
            )
            volume_name = f"treadmill-{deployment_id}-postgres-data"
            mounts[volume_name] = {
                "bind": "/var/lib/postgresql/data",
                "mode": "rw",
            }

        return mounts

    def _ensure_image(self, image: str) -> None:
        """Make sure *image* is available locally — pull it if it isn't, with
        a helpful error message when both fail. Local-only tags
        (``:dev``, ``:local``) are not pulled — they must be built locally
        before ``up``."""
        try:
            self.docker.images.get(image)
            return
        except docker.errors.ImageNotFound:
            pass
        if image.endswith((":dev", ":local")):
            console.print(
                f"[red]Image {image} not found locally. "
                "Build it before `up` (it carries a local-only tag).[/red]"
            )
            raise docker.errors.ImageNotFound(image)
        try:
            console.print(f"• Pulling [cyan]{image}[/cyan] …")
            self.docker.images.pull(image)
        except docker.errors.APIError as exc:
            console.print(
                f"[red]Image {image} not found locally and pull failed: {exc}.[/red]"
            )
            raise

    def _start_services(self) -> None:
        """Start each non-autoscaled ECS Service as a long-running container.

        Service containers carry the ``treadmill.role=service`` label and
        use predictable names (the family name) so they're DNS-discoverable
        on the docker network. ``up`` is idempotent: an already-running
        service is left alone; an exited service is removed and restarted.
        """
        if not self.state.service_specs:
            return
        for svc in self.state.service_specs:
            for cspec in svc.container_specs:
                self._start_service_container(cspec, svc.port_mappings)

    def _start_service_container(
        self,
        spec: ContainerSpec,
        port_mappings: list[tuple[int, int]],
    ) -> Container | None:
        """Start one service container (idempotent). Name = family.

        Returns the running container, or None if start was skipped."""
        # Family name doubles as container name and DNS hostname on the network.
        name = spec.family
        try:
            existing = self.docker.containers.get(name)
            if existing.status == "running":
                console.print(f"• Service [cyan]{name}[/cyan] already running.")
                return existing
            existing.remove(force=True)
        except docker.errors.NotFound:
            pass
        return self._run_container(
            spec,
            name=name,
            role="service",
            port_mappings=port_mappings,
        )

    @staticmethod
    def logs(container: str, *, follow: bool = False) -> None:
        client = docker.from_env()
        if container == "all":
            containers = client.containers.list(filters={"label": f"{LABEL_KEY}=true"})
        else:
            containers = [client.containers.get(container)]
        for c in containers:
            console.rule(f"[bold]{c.name}[/bold]")
            if follow:
                for line in c.logs(stream=True, follow=True):
                    print(line.decode("utf-8", errors="replace"), end="")
            else:
                print(c.logs(tail=200).decode("utf-8", errors="replace"))

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _synth(self) -> SynthResult:
        console.print("• Running cdk synth …")
        return synth(self.infra_dir)

    def _ensure_images_built(self) -> None:
        """Rebuild ``treadmill-api:dev`` and ``treadmill-agent:dev``
        from current source before any container references them.

        Docker's layer cache makes this a sub-second no-op when nothing
        has changed, so we always invoke ``docker build`` rather than
        checking timestamps — manifest-comparison logic adds complexity
        without saving measurable time on a cached build. The cure for
        "I ran stale code by accident" (see Phase E.1 part-2 in
        ``docs/plans/2026-05-13-week-4-dev-local-deployment.md``) is to
        make the rebuild happen automatically every time, not to add
        another lever the operator has to remember.

        Build context details:

        - API: ``docker build -t treadmill-api:dev .`` from
          ``services/api/`` — the Dockerfile uses paths relative to that
          directory.
        - Agent: ``docker build -t treadmill-agent:dev -f
          workers/agent/Dockerfile .`` from the **repo root** — the
          Dockerfile ``COPY``s ``services/api/...`` and
          ``workers/agent/...`` because the agent's ``pyproject.toml``
          declares ``treadmill-api`` as a workspace source.

        On failure the build's combined stdout+stderr is printed and
        ``RuntimeError`` is raised — we do **not** fall through to
        starting containers with a stale image, because that's the exact
        silent-bug we're protecting against.

        Skipped when ``self.build_images`` is False (the ``--no-build``
        CLI flag).
        """
        if not self.build_images:
            console.print(
                "[dim]• Skipping image rebuild (--no-build).[/dim]"
            )
            return

        repo_root = find_repo_root()
        builds: list[tuple[str, list[str], Path]] = [
            (
                DEV_LOCAL_API_IMAGE,
                ["docker", "build", "-t", DEV_LOCAL_API_IMAGE, "."],
                repo_root / "services" / "api",
            ),
            (
                DEV_LOCAL_AGENT_IMAGE,
                [
                    "docker", "build",
                    "-t", DEV_LOCAL_AGENT_IMAGE,
                    "-f", "workers/agent/Dockerfile",
                    ".",
                ],
                repo_root,
            ),
        ]
        for image, cmd, cwd in builds:
            console.print(f"• Building [cyan]{image}[/cyan] …")
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                check=False,
                # Capture so we only surface noise on failure — a
                # successful cached build emits a wall of "CACHED" lines
                # that drown the rest of ``up``'s progress block.
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                console.print(
                    f"[red]docker build failed for {image} "
                    f"(exit {result.returncode}). "
                    f"Build context: {cwd}[/red]"
                )
                if result.stdout:
                    console.print("[red]--- docker build stdout ---[/red]")
                    console.print(result.stdout)
                if result.stderr:
                    console.print("[red]--- docker build stderr ---[/red]")
                    console.print(result.stderr)
                raise RuntimeError(
                    f"docker build failed for {image}; refusing to start "
                    "containers with a stale image. Re-run with "
                    "``--no-build`` to bypass."
                )

    def _ensure_network(self) -> None:
        try:
            net = self.docker.networks.get(NETWORK_NAME)
            console.print(f"• Network [cyan]{NETWORK_NAME}[/cyan] exists.")
        except docker.errors.NotFound:
            net = self.docker.networks.create(
                NETWORK_NAME,
                driver="bridge",
                labels={LABEL_KEY: "true"},
            )
            console.print(f"• Network [cyan]{NETWORK_NAME}[/cyan] created.")
        self.state.network_id = net.id

    def _remove_network(self) -> None:
        try:
            net = self.docker.networks.get(NETWORK_NAME)
            net.remove()
            console.print(f"• Network [cyan]{NETWORK_NAME}[/cyan] removed.")
        except docker.errors.NotFound:
            pass

    def _start_moto(self) -> None:
        existing = self.docker.containers.list(
            all=True, filters={"name": MOTO_CONTAINER_NAME}
        )
        if existing:
            container = existing[0]
            if container.status != "running":
                container.start()
                console.print(f"• Started existing moto container ({container.short_id}).")
            else:
                console.print(f"• Moto container already running ({container.short_id}).")
        else:
            try:
                self.docker.images.get(MOTO_IMAGE)
            except docker.errors.ImageNotFound:
                console.print(f"• Pulling [cyan]{MOTO_IMAGE}[/cyan] …")
                self.docker.images.pull(MOTO_IMAGE)
            container = self.docker.containers.run(
                MOTO_IMAGE,
                name=MOTO_CONTAINER_NAME,
                detach=True,
                ports={f"{MOTO_CONTAINER_PORT}/tcp": MOTO_HOST_PORT},
                network=NETWORK_NAME,
                environment={"MOTO_PORT": str(MOTO_CONTAINER_PORT)},
                labels={LABEL_KEY: "true", "treadmill.role": "moto"},
            )
            console.print(f"• Moto container started ({container.short_id}).")
        self.state.moto_endpoint = f"http://localhost:{MOTO_HOST_PORT}"

    def _wait_for_moto(self, timeout: float = 30.0) -> None:
        import urllib.request

        assert self.state.moto_endpoint is not None
        deadline = time.monotonic() + timeout
        url = f"{self.state.moto_endpoint}/moto-api/"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        console.print(f"• Moto ready at {self.state.moto_endpoint}.")
                        return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f"moto did not become ready within {timeout}s")

    def _provision(self, result: SynthResult) -> None:
        assert self.state.moto_endpoint is not None
        provisioner = MotoProvisioner(endpoint_url=self.state.moto_endpoint)
        provisioner.provision(result)
        self.state.refs = provisioner.get_refs()
        network = LocalNetworkConfig(
            network_name=NETWORK_NAME,
            moto_container_name=MOTO_CONTAINER_NAME,
            moto_internal_port=MOTO_CONTAINER_PORT,
            moto_host_port=MOTO_HOST_PORT,
        )
        self.state.container_specs = resolve_task_definitions(
            result, self.state.refs, network
        )
        self.state.service_specs = resolve_services(
            result, self.state.refs, network
        )

    def _managed_containers(self) -> list[Container]:
        return self.docker.containers.list(
            all=True, filters={"label": f"{LABEL_KEY}=true"}
        )

    def _stop_managed_containers(self) -> None:
        containers = self._managed_containers()
        for c in containers:
            try:
                if c.status == "running":
                    c.stop(timeout=5)
                c.remove()
                console.print(f"• Removed [cyan]{c.name}[/cyan].")
            except docker.errors.NotFound:
                pass

    def _report_up(self) -> None:
        console.rule("[bold green]Treadmill local — ready[/bold green]")
        console.print(f"  moto endpoint: [cyan]{self.state.moto_endpoint}[/cyan]")
        console.print(
            "  AWS clients: set "
            f"[cyan]AWS_ENDPOINT_URL={self.state.moto_endpoint}[/cyan]"
        )
        if AUTOSCALER_PID_FILE.exists():
            pid = AUTOSCALER_PID_FILE.read_text().strip()
            console.print(f"  autoscaler:    pid={pid}, log={AUTOSCALER_LOG_FILE}")

    # ── Autoscaler lifecycle ──────────────────────────────────────────────────

    def _start_autoscaler(self) -> None:
        """Spawn the autoscaler as a detached subprocess.

        The subprocess writes its PID to ``.treadmill-local/autoscaler.pid``
        so a later ``down`` invocation (or a separate process) can find and
        signal it. stdout / stderr go to ``.treadmill-local/autoscaler.log``.
        """
        from treadmill_local.autoscaler import parse_scalable_target_bounds

        if self.state.refs is None or self.state.container_specs is None:
            raise RuntimeError("autoscaler requires the runtime to be provisioned first")

        # If one is already running, leave it alone.
        if self._autoscaler_pid_alive():
            console.print("• Autoscaler already running.")
            return

        # Read bounds and queue URL from the synthesized template — CDK is the
        # single source of truth, the adapter is just an interpreter.
        result = self._synth()
        scalable_targets = result.by_type("AWS::ApplicationAutoScaling::ScalableTarget")
        if not scalable_targets:
            console.print("[yellow]• No ScalableTarget in CDK; autoscaler not started.[/yellow]")
            return
        min_count, max_count = parse_scalable_target_bounds(scalable_targets[0].properties)

        if not self.state.container_specs:
            console.print("[yellow]• No ECS task definitions; autoscaler not started.[/yellow]")
            return
        family = self.state.container_specs[0].family

        sqs_queues = result.by_type("AWS::SQS::Queue")
        queue_url = self.state.refs.get(sqs_queues[0].logical_id) if sqs_queues else None
        if queue_url is None:
            console.print("[yellow]• No SQS queue in stack; autoscaler not started.[/yellow]")
            return

        STATE_DIR.mkdir(exist_ok=True)
        log_handle = open(AUTOSCALER_LOG_FILE, "ab")
        env = {
            **os.environ,
            "TREADMILL_INFRA_DIR": str(self.infra_dir),
            "TREADMILL_AUTOSCALER_FAMILY": family,
            "TREADMILL_AUTOSCALER_QUEUE_URL": queue_url,
            "TREADMILL_AUTOSCALER_MIN": str(min_count),
            "TREADMILL_AUTOSCALER_MAX": str(max_count),
            "TREADMILL_AUTOSCALER_TICK_SECONDS": "2",
            "AWS_ENDPOINT_URL": self.state.moto_endpoint or "",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "treadmill_local.autoscaler"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(Path.cwd()),
        )
        AUTOSCALER_PID_FILE.write_text(str(proc.pid))
        console.print(
            f"• Autoscaler started (pid={proc.pid}, family={family}, "
            f"min={min_count}, max={max_count})."
        )

    def _start_autoscaler_dev_local(self) -> None:
        """Spawn the autoscaler subprocess for dev-local mode (ADR-0018).

        Mirrors ``_start_autoscaler`` (the fully-local equivalent) but
        sources its config from the deployment YAML rather than the
        synthesized CDK template — dev-local has no CDK compute stack to
        introspect. The PID file + log file path is the same as
        fully-local (single-deployment v0; multi-deployment will need
        deployment-suffixed paths — flagged here as the obvious upgrade
        point).

        Env-vars passed to the subprocess:
          * ``TREADMILL_INFRA_DIR`` — infra dir (for legacy parity).
          * ``TREADMILL_AUTOSCALER_FAMILY`` — always ``treadmill-agent`` at v0.
          * ``TREADMILL_AUTOSCALER_QUEUE_URL`` — from ``aws.work_queue_url``.
          * ``TREADMILL_AUTOSCALER_MIN`` / ``_MAX`` / ``_TICK_SECONDS`` — YAML.
          * ``TREADMILL_AUTOSCALER_DEPLOYMENT_ID`` — the deployment slug;
            the subprocess uses this to construct a ``LocalRuntime`` with
            the right ``deployment_config`` (so ``start_worker_once``
            triggers the host-side credential fetch per ADR-0019).
          * ``AWS_PROFILE`` — inherited from parent or from YAML.
          * ``AWS_DEFAULT_REGION`` — from YAML.
          * NOTABLY ABSENT: ``AWS_ENDPOINT_URL`` (that's the moto override;
            dev-local talks to real AWS).
        """
        assert self.deployment_config is not None
        cfg = self.deployment_config

        if self._autoscaler_pid_alive():
            console.print("• Autoscaler already running.")
            return

        autoscaler_cfg = cfg["autoscaler"]
        queue_url = cfg["aws"]["work_queue_url"]
        family = AGENT_FAMILY  # v0: one family in dev-local (ADR-0018).
        deployment_id = cfg["deployment_id"]

        STATE_DIR.mkdir(exist_ok=True)
        # Single-deployment v0: PID and log files are NOT suffixed by
        # deployment_id. When multi-deployment lands (operator running
        # personal + employer concurrently), these need
        # deployment-suffixed paths so the two scalers don't collide.
        log_handle = open(AUTOSCALER_LOG_FILE, "ab")
        env = {
            **os.environ,
            "TREADMILL_INFRA_DIR": str(self.infra_dir),
            "TREADMILL_AUTOSCALER_FAMILY": family,
            "TREADMILL_AUTOSCALER_QUEUE_URL": queue_url,
            "TREADMILL_AUTOSCALER_MIN": str(autoscaler_cfg["min"]),
            "TREADMILL_AUTOSCALER_MAX": str(autoscaler_cfg["max"]),
            "TREADMILL_AUTOSCALER_TICK_SECONDS": str(
                autoscaler_cfg["tick_seconds"]
            ),
            # The subprocess entrypoint branches on this env var: when set
            # it constructs ``LocalRuntime(deployment_config=cfg)`` so
            # ``start_worker_once`` runs the dev-local credential-injection
            # path (ADR-0019); when unset the legacy moto path runs.
            "TREADMILL_AUTOSCALER_DEPLOYMENT_ID": deployment_id,
            # AWS routing — real AWS, no moto override. ``AWS_PROFILE``
            # is inherited from the parent shell (operator's SSO);
            # ``AWS_DEFAULT_REGION`` is set explicitly from the YAML so
            # boto3 in the subprocess doesn't fall back to the operator's
            # shell default.
            "AWS_DEFAULT_REGION": cfg["aws_region"],
            "AWS_PROFILE": os.environ.get("AWS_PROFILE", cfg["aws_profile"]),
        }
        # Defensive: drop the moto endpoint override if it somehow leaked
        # into the parent env. dev-local must talk to real AWS.
        env.pop("AWS_ENDPOINT_URL", None)

        proc = subprocess.Popen(
            [sys.executable, "-m", "treadmill_local.autoscaler"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(Path.cwd()),
        )
        AUTOSCALER_PID_FILE.write_text(str(proc.pid))
        console.print(
            f"• Autoscaler started (pid={proc.pid}, family={family}, "
            f"min={autoscaler_cfg['min']}, max={autoscaler_cfg['max']}, "
            f"tick={autoscaler_cfg['tick_seconds']}s, "
            f"deployment={deployment_id})."
        )

    def _stop_autoscaler(self) -> None:
        if not AUTOSCALER_PID_FILE.exists():
            return
        try:
            pid = int(AUTOSCALER_PID_FILE.read_text().strip())
        except ValueError:
            AUTOSCALER_PID_FILE.unlink(missing_ok=True)
            return
        if self._pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                # Brief grace period; the autoscaler honors SIGTERM.
                for _ in range(20):
                    if not self._pid_alive(pid):
                        break
                    time.sleep(0.1)
                if self._pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
                console.print(f"• Autoscaler stopped (pid={pid}).")
            except ProcessLookupError:
                pass
        AUTOSCALER_PID_FILE.unlink(missing_ok=True)

    def _autoscaler_pid_alive(self) -> bool:
        if not AUTOSCALER_PID_FILE.exists():
            return False
        try:
            pid = int(AUTOSCALER_PID_FILE.read_text().strip())
        except ValueError:
            return False
        return self._pid_alive(pid)

    def _autoscaler_pulse_status(self) -> str:
        """Return a rich-renderable status string for the autoscaler row.

        Uses the autoscaler log file's mtime as the liveness signal —
        the autoscaler logs ``tick:`` at INFO on every loop iteration
        (per-2026-05-18 refactor following the heartbeat-reinvented-
        logging learning). Fresh mtime (within ``tick_seconds × 5``)
        means the loop is ticking; stale mtime means the silent-death
        failure mode captured 2026-05-17.

        Tick interval is read from ``deployment_config.autoscaler.tick_seconds``
        when present (dev-local) and falls back to 2.0s (fully-local default).
        """
        if not AUTOSCALER_LOG_FILE.exists():
            return "[yellow]running (no log yet)[/yellow]"
        tick = 2.0
        if self.deployment_config is not None:
            tick = float(self.deployment_config["autoscaler"]["tick_seconds"])
        age = time.time() - AUTOSCALER_LOG_FILE.stat().st_mtime
        threshold = tick * 5
        if age > threshold:
            return f"[red]stale ({age:.0f}s since last tick log)[/red]"
        return f"running (last tick {age:.0f}s ago)"

    # ── Scheduler lifecycle ───────────────────────────────────────────────────

    def _start_scheduler_dev_local(self) -> None:
        """Spawn the scheduler subprocess for dev-local mode.

        Mirrors ``_start_autoscaler_dev_local`` but drives
        ``treadmill_api.scheduler.runner`` — the asyncio cron-dispatch
        loop — rather than the autoscaler. Not started in fully-local
        (moto) mode: the scheduler needs a real Postgres schema and
        the host-side port mapping that only exists in dev-local.

        Env-vars passed to the subprocess:
          * ``DATABASE_URL`` — host-side Postgres URL (localhost:15432)
            so the subprocess reaches the container from the host.
          * ``EVENTS_TOPIC_ARN`` — from ``aws.events_topic_arn``.
          * ``AWS_DEFAULT_REGION`` — from YAML.
          * ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` — the
            API's IAM-User keys (same principal that can publish to
            SNS). Fetched once on the host by
            ``_ensure_dev_local_credentials`` (ADR-0019).
          * NOTABLY ABSENT: ``AWS_ENDPOINT_URL`` (moto override;
            dev-local talks to real AWS).
        """
        assert self.deployment_config is not None
        cfg = self.deployment_config

        if self._scheduler_pid_alive():
            console.print("• Scheduler already running.")
            return

        # Credentials must already be fetched (called after _ensure_dev_local_credentials).
        self._ensure_dev_local_credentials()
        assert self._api_aws_env is not None

        deployment_id = cfg["deployment_id"]
        STATE_DIR.mkdir(exist_ok=True)
        log_handle = open(SCHEDULER_LOG_FILE, "ab")
        env = {
            **os.environ,
            # Host-side Postgres URL — the subprocess runs on the host, so it
            # uses the mapped port (15432) rather than the container-DNS form.
            "DATABASE_URL": _SCHEDULER_HOST_DB_URL,
            "EVENTS_TOPIC_ARN": cfg["aws"]["events_topic_arn"],
            "AWS_DEFAULT_REGION": cfg["aws_region"],
            # Reuse the API's IAM-User keys — same principal, same SNS permissions.
            **self._api_aws_env,
        }
        # Defensive: drop the moto endpoint override if it leaked into the env.
        env.pop("AWS_ENDPOINT_URL", None)

        proc = subprocess.Popen(
            [sys.executable, "-m", "treadmill_api.scheduler.runner"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(Path.cwd()),
        )
        SCHEDULER_PID_FILE.write_text(str(proc.pid))
        console.print(
            f"• Scheduler started (pid={proc.pid}, deployment={deployment_id})."
        )

    def _stop_scheduler(self) -> None:
        if not SCHEDULER_PID_FILE.exists():
            return
        try:
            pid = int(SCHEDULER_PID_FILE.read_text().strip())
        except ValueError:
            SCHEDULER_PID_FILE.unlink(missing_ok=True)
            return
        if self._pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    if not self._pid_alive(pid):
                        break
                    time.sleep(0.1)
                if self._pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
                console.print(f"• Scheduler stopped (pid={pid}).")
            except ProcessLookupError:
                pass
        SCHEDULER_PID_FILE.unlink(missing_ok=True)

    def _scheduler_pid_alive(self) -> bool:
        if not SCHEDULER_PID_FILE.exists():
            return False
        try:
            pid = int(SCHEDULER_PID_FILE.read_text().strip())
        except ValueError:
            return False
        return self._pid_alive(pid)

    # ── Deploy-watcher lifecycle ──────────────────────────────────────────────

    def _start_deploy_watcher_dev_local(self) -> None:
        """Spawn the deploy-watcher subprocess for dev-local mode.

        Mirrors ``_start_autoscaler_dev_local`` but drives
        ``treadmill_local.deploy_watcher`` — the SQS-polling loop that
        reconciles local containers when PRs are merged. Not started in
        fully-local (moto) mode: the watcher polls real AWS SQS and needs
        the deployment YAML that only exists in dev-local.

        Env-vars passed to the subprocess:
          * ``TREADMILL_DEPLOY_WATCHER_DEPLOYMENT_ID`` — the deployment
            slug; the subprocess's ``main()`` branches on this to load
            the right per-deployment YAML and extract
            ``deploy_events_queue_url``.
          * ``AWS_PROFILE`` — inherited from parent shell or YAML fallback.
          * ``AWS_DEFAULT_REGION`` — from YAML.
          * ``GITHUB_TOKEN`` — fetched once on the host by
            ``_ensure_dev_local_credentials`` and injected here so the
            subprocess can call the GitHub PR-files API without reading
            from Secrets Manager itself.
          * ``GITHUB_OWNER`` / ``GITHUB_REPO`` — parsed from the local
            checkout's ``origin`` remote so the watcher can build PR-files
            API URLs without the operator exporting anything by hand.
          * ``TREADMILL_REPO_ROOT`` — absolute path to the same checkout
            the watcher reconciles against on PR-merged events.
          * NOTABLY ABSENT: ``AWS_ENDPOINT_URL`` (moto override;
            dev-local talks to real AWS).
        """
        assert self.deployment_config is not None
        cfg = self.deployment_config

        if self._deploy_watcher_pid_alive():
            console.print("• Deploy watcher already running.")
            return

        deployment_id = cfg["deployment_id"]

        # Credentials must already be fetched (called after _ensure_dev_local_credentials).
        self._ensure_dev_local_credentials()

        repo_root = find_repo_root()
        github_owner, github_repo = parse_github_origin(repo_root)

        STATE_DIR.mkdir(exist_ok=True)
        log_handle = open(DEPLOY_WATCHER_LOG_FILE, "ab")
        env = {
            **os.environ,
            "TREADMILL_DEPLOY_WATCHER_DEPLOYMENT_ID": deployment_id,
            "AWS_DEFAULT_REGION": cfg["aws_region"],
            "AWS_PROFILE": os.environ.get("AWS_PROFILE", cfg["aws_profile"]),
            "GITHUB_OWNER": github_owner,
            "GITHUB_REPO": github_repo,
            "TREADMILL_REPO_ROOT": str(repo_root),
        }
        # ADR-0049: in App mode the watcher mints installation tokens from the
        # API (reachable on the host port) per call instead of holding the PAT.
        if cfg.get("github_app_id") and cfg.get("secrets", {}).get(
            "github_app_private_key_secret_name"
        ):
            env["GITHUB_AUTH_MODE"] = "app"
            env["TREADMILL_API_URL"] = "http://localhost:8088"
        else:
            assert self._github_token is not None
            env["GITHUB_TOKEN"] = self._github_token
        # Defensive: drop the moto endpoint override if it leaked into the env.
        env.pop("AWS_ENDPOINT_URL", None)

        proc = subprocess.Popen(
            [sys.executable, "-m", "treadmill_local.deploy_watcher"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(Path.cwd()),
        )
        DEPLOY_WATCHER_PID_FILE.write_text(str(proc.pid))
        console.print(
            f"• Deploy watcher started (pid={proc.pid}, deployment={deployment_id})."
        )

    def _stop_deploy_watcher(self) -> None:
        if not DEPLOY_WATCHER_PID_FILE.exists():
            return
        try:
            pid = int(DEPLOY_WATCHER_PID_FILE.read_text().strip())
        except ValueError:
            DEPLOY_WATCHER_PID_FILE.unlink(missing_ok=True)
            return
        if self._pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    if not self._pid_alive(pid):
                        break
                    time.sleep(0.1)
                if self._pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
                console.print(f"• Deploy watcher stopped (pid={pid}).")
            except ProcessLookupError:
                pass
        DEPLOY_WATCHER_PID_FILE.unlink(missing_ok=True)

    def _deploy_watcher_pid_alive(self) -> bool:
        if not DEPLOY_WATCHER_PID_FILE.exists():
            return False
        try:
            pid = int(DEPLOY_WATCHER_PID_FILE.read_text().strip())
        except ValueError:
            return False
        return self._pid_alive(pid)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    # ── Observability stack lifecycle (ADR-0043) ──────────────────────────────

    def _start_observability_dev_local(self) -> None:
        """Bring up the observability compose stack for dev-local mode.

        Per ADR-0043, dev-local runs Loki + Prometheus + Tempo + Grafana
        + OTel Collector in a local docker-compose unit (rather than the
        AWS-hosted EC2 stack used by fully_remote). The base compose
        file at ``infra/observability/docker-compose.yml`` is reused
        verbatim from the production path; a sibling
        ``docker-compose.local.yml`` override swaps EBS bind mounts for
        named docker volumes, swaps Tempo's S3 backend for filesystem,
        and sets a sensible default Grafana admin password.

        Grafana's host-side port is driven by
        ``aws.observability_grafana_port`` in the deployment YAML
        (default 3001 — see ``OBSERVABILITY_GRAFANA_HOST_PORT_DEFAULT``)
        and injected via the ``GRAFANA_HOST_PORT`` env var that
        ``docker-compose.local.yml`` substitutes into the ports binding.
        This single-source-of-truth shape means the URL ``treadmill
        observe`` opens matches the binding compose actually creates,
        and operators can dodge a port-3000 collision (bunkhouse
        dashboard, Next.js dev server, etc.) by editing one YAML field.

        Idempotent: if the OTel collector container is already running
        the call is a noop. Container existence is the liveness signal
        — compose owns the lifecycle, so there's no PID file (in
        contrast to the autoscaler / scheduler / deploy-watcher
        subprocesses).

        Foreground exec (subprocess.run, not Popen): the first ``up``
        pulls ~1.5 GB of images and operators need to see what's
        happening rather than waiting in silence. Subsequent runs are
        sub-second when images are cached.
        """
        if self._observability_already_running():
            console.print("• Observability stack already running.")
            return

        repo_root = find_repo_root()
        base_file = repo_root / OBSERVABILITY_COMPOSE_FILE_REL
        local_file = repo_root / OBSERVABILITY_COMPOSE_LOCAL_FILE_REL
        cmd = [
            "docker", "compose",
            "-f", str(base_file),
            "-f", str(local_file),
            "up", "-d",
        ]
        # Resolve the operator-facing Grafana port from the deployment
        # YAML, falling back to 3001 when the field is absent (older
        # YAMLs that pre-date this knob). This env var is what
        # ``docker-compose.local.yml`` substitutes into the host-side
        # ports binding; without it the default ``${GRAFANA_HOST_PORT:-3001}``
        # still kicks in, but we pass it explicitly so the value the
        # operator wrote in YAML is the one compose binds.
        grafana_port = self._observability_grafana_port()
        env = {**os.environ, "GRAFANA_HOST_PORT": str(grafana_port)}
        grafana_url = f"http://localhost:{grafana_port}"
        console.print(
            "• Starting observability stack "
            f"(compose: {base_file.name} + {local_file.name})..."
        )
        try:
            subprocess.run(cmd, check=True, env=env)
        except subprocess.CalledProcessError as exc:
            console.print(
                "[red]• Observability stack failed to start "
                f"(docker compose exit {exc.returncode}). "
                f"Common causes: port conflict on "
                f"{grafana_port}/3100/4317/4318/8888/9090, "
                "docker daemon not running, or first-pull network failure. "
                f"To pick a different Grafana port, edit "
                f"``aws.observability_grafana_port`` in "
                f"``~/.treadmill/<deployment>.yaml``. "
                "Re-run after resolving, or pass --no-observability to skip.[/red]"
            )
            raise
        console.print(
            f"• Observability stack ready (Grafana {grafana_url}, "
            f"OTLP {OBSERVABILITY_OTLP_HTTP_URL})."
        )

    def _observability_grafana_port(self) -> int:
        """Return the host-side Grafana port from the deployment YAML.

        Reads ``cfg["aws"]["observability_grafana_port"]`` and coerces
        to int; falls back to ``OBSERVABILITY_GRAFANA_HOST_PORT_DEFAULT``
        when the field is absent (older YAMLs pre-dating this knob, or
        a fully-local invocation that nevertheless reached this method
        via test wiring). The compose file's own
        ``${GRAFANA_HOST_PORT:-3001}`` default backs us up if the env
        var is somehow unset, but passing the explicit value here means
        the in-process Python sees the same number compose binds.
        """
        if self.deployment_config is None:
            return OBSERVABILITY_GRAFANA_HOST_PORT_DEFAULT
        raw = self.deployment_config.get("aws", {}).get(
            "observability_grafana_port"
        )
        if raw is None:
            return OBSERVABILITY_GRAFANA_HOST_PORT_DEFAULT
        return int(raw)

    def _stop_observability(self) -> None:
        """Tear down the observability compose stack.

        Runs ``docker compose down`` without ``-v`` so the named volumes
        (treadmill-loki-data, treadmill-prometheus-data,
        treadmill-tempo-data) persist across down/up cycles — matching
        the Postgres named-volume convention. Operators who want to
        reclaim disk drop the volumes by hand.

        Idempotent: compose ``down`` against an absent stack is a noop
        with a brief warning; we swallow it (best-effort teardown).
        """
        if not self._observability_already_running():
            return
        repo_root = find_repo_root()
        base_file = repo_root / OBSERVABILITY_COMPOSE_FILE_REL
        local_file = repo_root / OBSERVABILITY_COMPOSE_LOCAL_FILE_REL
        cmd = [
            "docker", "compose",
            "-f", str(base_file),
            "-f", str(local_file),
            "down",
        ]
        try:
            subprocess.run(cmd, check=True)
            console.print("• Observability stack stopped.")
        except subprocess.CalledProcessError as exc:
            # Best-effort: log and continue so the rest of `down` still
            # cleans up (containers, network). A failed compose down is
            # usually a transient docker daemon hiccup, not something
            # that should block other teardown.
            console.print(
                f"[yellow]• Observability stack down returned exit "
                f"{exc.returncode}; continuing.[/yellow]"
            )

    def _observability_already_running(self) -> bool:
        """True iff the OTel-collector container is up.

        OTel collector is the load-bearing stack member from the worker
        perspective (it's where OTLP traffic terminates), so its
        presence is the cleanest proxy for the whole compose unit. The
        operator could in principle stop one of the five services by
        hand, but at that point manual recovery is appropriate.
        """
        try:
            container = self.docker.containers.get(OBSERVABILITY_OTEL_CONTAINER_NAME)
        except docker.errors.NotFound:
            return False
        return container.status == "running"

