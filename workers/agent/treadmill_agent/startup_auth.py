"""GitHub auth bootstrap for the worker.

Per ADR-0019, the worker's AWS credentials arrive as
``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` env vars injected by
the local-adapter at container-spawn time. The worker never authenticates
with SSO and never fetches its own credentials secret.

When ``REPO_MODE=github`` the worker authenticates to GitHub via ``gh``'s
keyring. Two modes (ADR-0049):

  * ``GITHUB_AUTH_MODE=pat`` (legacy): fetch the personal PAT from Secrets
    Manager and hand it to ``gh``.
  * ``GITHUB_AUTH_MODE=app``: ask the API to mint a short-lived GitHub App
    installation token (the App private key stays on the API, never on the
    worker) and hand *that* to ``gh``. This is what lets the personal PAT be
    decommissioned.

Either way the token is handed to ``gh`` via stdin so it never appears in
argv, env, or git URLs, then dropped.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    from treadmill_agent.config import Settings

logger = logging.getLogger("treadmill.agent.startup_auth")


class StartupAuthError(RuntimeError):
    """Worker bootstrap failed in a way the runner cannot recover from."""


def resolve_worker_aws_session(settings: "Settings") -> boto3.session.Session:
    """Return the boto3 session the worker uses for every AWS call.

    Per ADR-0019: the local-adapter injects the worker's IAM-User keys
    into the container as env vars before the worker process starts.
    Boto3's default credential chain reads them from the environment;
    this function just returns a region-scoped session.
    """
    return boto3.Session(region_name=settings.aws_region)


def _apply_token_to_gh(token: str) -> None:
    """Hand a token to ``gh`` via stdin and install the git credential helper.

    Shared by both the PAT and App paths. ``input=`` routes the token through
    stdin — the supported channel for ``gh auth login --with-token`` — so it
    never lands in argv (``/proc/<pid>/cmdline``) or the environment.
    """
    result = subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=token.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        raise StartupAuthError(
            f"`gh auth login --with-token` exited {result.returncode}: "
            f"{result.stderr.decode(errors='replace')}"
        )
    # Install the credential helper so plain ``git clone https://github.com/...``
    # URLs route through ``gh``.
    result = subprocess.run(["gh", "auth", "setup-git"], capture_output=True)
    if result.returncode != 0:
        raise StartupAuthError(
            f"`gh auth setup-git` exited {result.returncode}: "
            f"{result.stderr.decode(errors='replace')}"
        )


def bootstrap_github_auth(
    *,
    settings: "Settings",
    aws_session: boto3.session.Session,
) -> None:
    """Legacy PAT path: fetch the PAT from Secrets Manager and hand it to ``gh``."""
    secret_name = settings.github_pat_secret_name
    if not secret_name:
        raise StartupAuthError(
            "repo_mode='github' (pat) requires GITHUB_PAT_SECRET_NAME to be set"
        )
    logger.info("fetching GitHub PAT from Secrets Manager: secret=%s", secret_name)
    secrets = aws_session.client("secretsmanager")
    try:
        resp = secrets.get_secret_value(SecretId=secret_name)
    except Exception as exc:  # noqa: BLE001
        raise StartupAuthError(
            f"failed to fetch GitHub PAT secret {secret_name!r}: {exc}"
        ) from exc
    pat = resp.get("SecretString")
    if not pat:
        raise StartupAuthError(
            f"GitHub PAT secret {secret_name!r} has no SecretString"
        )
    try:
        _apply_token_to_gh(pat)
    finally:
        pat = None  # noqa: F841 - intentional dereference
    logger.info("gh auth bootstrap complete (PAT)")


def bootstrap_github_auth_via_app(
    *,
    settings: "Settings",
    repo: str | None = None,
) -> None:
    """App path: mint a short-lived installation token from the API, hand to ``gh``.

    The worker never holds the App private key — it POSTs to the API's
    ``/api/v1/github/installation-token`` and applies the returned token. Uses
    stdlib ``urllib`` so the worker takes on no new dependency.

    When ``repo`` is ``None`` (the startup home-token bootstrap), POSTs an
    empty body so the API returns a token for the sole installation. When
    ``repo`` is set (``owner/name``), POSTs ``{"repo": repo}`` so the API
    scopes the token to that repo's installation — used by the runner per
    task once ``ctx.repo`` is known.
    """
    url = settings.api_url.rstrip("/") + "/api/v1/github/installation-token"
    body = {"repo": repo} if repo else {}
    if repo:
        logger.info(
            "minting GitHub App installation token via API: %s repo=%s",
            url, repo,
        )
    else:
        logger.info("minting GitHub App installation token via API: %s", url)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        raise StartupAuthError(
            f"failed to mint installation token via {url}: {exc}"
        ) from exc
    token = payload.get("token")
    if not token:
        raise StartupAuthError("installation-token response had no 'token'")
    try:
        _apply_token_to_gh(token)
    finally:
        token = None  # noqa: F841 - intentional dereference
    if repo:
        logger.info(
            "gh auth bootstrap complete (GitHub App installation token, repo=%s)",
            repo,
        )
    else:
        logger.info("gh auth bootstrap complete (GitHub App installation token)")
