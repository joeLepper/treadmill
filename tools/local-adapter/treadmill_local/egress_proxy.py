"""Egress proxy lifecycle helpers for the autoscaler (ADR-0060).

Encapsulates:
  - treadmill-egress internal bridge network creation
  - proxy container spawn (idempotent)
  - per-worker install credential minting
  - per-worker WorkerAllowlist JSON writing
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

from treadmill_local.docker_client import DockerClientAdapter

EGRESS_NETWORK_NAME = "treadmill-egress"
EGRESS_PROXY_CONTAINER_NAME = "treadmill-egress-proxy"
EGRESS_PROXY_IMAGE = "treadmill-egress-proxy:dev"

# Static always-allowed hostnames for every worker (ADR-0060).
# AWS regional service hostnames (SQS, SNS, SecretsManager) are
# appended via build_always_allowed() using AWS_DEFAULT_REGION so the
# same code targets any deployment.
#
# The proxy matches these via fnmatch glob — ``*.github.com`` matches
# any subdomain. Bare ``github.com`` is listed explicitly because
# glob wildcards do not match the parent domain (``*.github.com``
# does not match ``github.com`` itself); git clone over HTTPS hits
# the bare hostname, which broke at the 2026-06-03 wedge.
_ALWAYS_ALLOWED_STATIC: list[str] = [
    "api.anthropic.com",
    "api.github.com",
    "github.com",
    "*.github.com",
    "*.githubusercontent.com",
]

# AWS service hostnames the worker must reach to pick up work + read
# its credential context. Templated by region in build_always_allowed.
# Audit of worker-side boto3.client(...) call sites (workers/agent/):
#   - sqs           — runner.receive_message (claims work from queue)
#   - sns           — eventbus publish (step lifecycle events)
#   - secretsmanager— startup_auth (per-account Claude credential fetch, ADR-0055)
# SQS is the load-bearing one — without it the worker can't claim
# messages and exits 1 immediately on startup (the 2026-06-03 wedge).
# Listed as bare service names; the region suffix is appended at build
# time so any deployment region works without forking the list.
_AWS_SERVICE_NAMES: list[str] = [
    "sqs",
    "sns",
    "secretsmanager",
]

# Default install-allowed package registry hostnames merged into every worker.
INSTALL_DEFAULTS: list[str] = [
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "registry.yarnpkg.com",
]


def build_always_allowed() -> list[str]:
    """Return always_allowed list, appending the package registries from
    ``INSTALL_DEFAULTS``, ``TREADMILL_API_HOST`` when set, plus the
    region-templated AWS service hostnames from ``_AWS_SERVICE_NAMES``
    (read from ``AWS_DEFAULT_REGION``).
    """
    hosts = list(_ALWAYS_ALLOWED_STATIC)
    # 2026-06-04 — package registries are always-allowed, not just
    # install-phase: author-side validation gates use the standard
    # ``cd <pkg> && uv run pytest`` pattern, and ``uv run`` builds the
    # project venv at invocation time, downloading wheels from
    # pypi.org / files.pythonhosted.org. Phase-scoping these hosts broke
    # every uv-run validation gate fleet-wide ("tunnel error" → gate-broken
    # architect loops, e.g. task 57598e7a). No new trust is granted: the
    # same hosts were already reachable during the install phase.
    hosts.extend(INSTALL_DEFAULTS)
    api_host = os.environ.get("TREADMILL_API_HOST")
    if api_host:
        hosts.append(api_host)
    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
    if region:
        for svc in _AWS_SERVICE_NAMES:
            hosts.append(f"{svc}.{region}.amazonaws.com")
    return hosts


def build_install_allowed(download_urls: list[str]) -> list[str]:
    """Return INSTALL_DEFAULTS merged with per-repo binary download URL hostnames."""
    extra: set[str] = set()
    for url in download_urls:
        try:
            hostname = urlparse(url).hostname
            if hostname:
                extra.add(hostname)
        except Exception:
            pass
    return sorted(set(INSTALL_DEFAULTS) | extra)


def mint_worker_credential() -> tuple[str, str]:
    """Return (plaintext_token, sha256_hex) for a new per-worker install credential."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


# Internal docker-DNS hosts that workers must reach directly, NOT
# through the egress proxy. The proxy is CONNECT-only (ADR-0060):
# plain-HTTP requests to `http://treadmill-api:8088` (e.g. the
# GitHub-App installation-token mint at worker startup) return 400
# if routed through it. NO_PROXY bypasses the proxy for these
# hosts; lowercase `no_proxy` is also set because Python's urllib
# honors that form in some code paths.
_WORKER_INTERNAL_HOSTS = "treadmill-api,localhost,127.0.0.1"


def worker_proxy_env(install_credential: str) -> dict[str, str]:
    """Env dict to inject on each worker so HTTP/HTTPS go through the
    egress proxy while internal Treadmill services stay direct.

    Centralizes the per-worker proxy contract so the autoscaler call
    site and the test suite agree on a single shape.
    """
    return {
        "HTTP_PROXY": "http://treadmill-egress-proxy:3128",
        "HTTPS_PROXY": "http://treadmill-egress-proxy:3128",
        "NO_PROXY": _WORKER_INTERNAL_HOSTS,
        "no_proxy": _WORKER_INTERNAL_HOSTS,
        "TREADMILL_INSTALL_PROXY_TOKEN": install_credential,
    }


def ensure_egress_network(adapter: DockerClientAdapter) -> None:
    """Ensure the treadmill-egress internal bridge network exists."""
    adapter.ensure_network(EGRESS_NETWORK_NAME, internal=True)


def ensure_egress_proxy_container(
    adapter: DockerClientAdapter,
    config_dir: Path,
) -> None:
    """Spawn the egress proxy container if it is not already running. Idempotent.

    ADR-0064 Step 2: the proxy is multi-attached to the ``treadmill-local``
    network after creation so its outbound CONNECTs route through that
    network's gateway (the ``treadmill-egress`` bridge is ``internal=True``
    and has no upstream gateway). The runtime constant
    ``NETWORK_NAME`` is imported here (rather than hardcoded) to avoid
    magic-string drift between the two modules.
    """
    if adapter.container_running(EGRESS_PROXY_CONTAINER_NAME):
        return
    proxy_container = adapter.run_container(
        EGRESS_PROXY_IMAGE,
        name=EGRESS_PROXY_CONTAINER_NAME,
        detach=True,
        network=EGRESS_NETWORK_NAME,
        volumes={str(config_dir): {"bind": "/etc/egress-proxy-config", "mode": "ro"}},
    )
    # Imported lazily to avoid a circular import — runtime.py imports
    # from this module on dev-local boot.
    from treadmill_local.runtime import NETWORK_NAME

    adapter.connect_container_to_network(NETWORK_NAME, proxy_container)


def write_worker_allowlist(
    config_dir: Path,
    worker_ip: str,
    credential_hash: str,
    always_allowed: list[str],
    install_allowed: list[str],
) -> None:
    """Write per-worker allowlist config picked up by the proxy via mtime watch."""
    config_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "worker_ip": worker_ip,
        "always_allowed": always_allowed,
        "install_allowed": install_allowed,
        "install_credential_hash": credential_hash,
    }
    (config_dir / f"{worker_ip}.json").write_text(json.dumps(data, indent=2))
