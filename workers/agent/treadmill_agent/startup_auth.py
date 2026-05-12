"""GitHub PAT bootstrap for the worker.

Per ADR-0019, the worker's AWS credentials arrive as
``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` env vars injected by
the local-adapter at container-spawn time. The worker never authenticates
with SSO and never fetches its own credentials secret — its boto3
session is just ``boto3.Session(region_name=...)``, with the standard
env-var credential resolution picking up the injected keys.

When ``REPO_MODE=github``, the worker still fetches the GitHub PAT from
Secrets Manager at startup (using the same env-var-resolved boto3
session) and hands it to ``gh`` via stdin so subsequent ``git`` /
``gh`` calls authenticate via ``gh``'s keyring — no PAT in argv, env,
or git URLs.
"""

from __future__ import annotations

import logging
import subprocess
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
    this function just returns a region-scoped session and lets that
    standard resolution happen.

    The previous bootstrap-then-worker pattern (fetch a credentials
    secret from Secrets Manager using a default-chain session, then
    rebuild a session from the fetched keys) is gone — that path
    required ``~/.aws`` mounted into the container, which broke on the
    SSO-cache-refresh writeback path. See ADR-0019 for the full story.
    """
    return boto3.Session(region_name=settings.aws_region)


def bootstrap_github_auth(
    *,
    settings: "Settings",
    aws_session: boto3.session.Session,
) -> None:
    """Fetch the PAT from Secrets Manager and hand it to ``gh``.

    Called once at worker startup when ``repo_mode='github'``. The PAT
    is held in a local variable for the duration of two subprocess
    calls (``gh auth login --with-token`` + ``gh auth setup-git``) and
    is then dereferenced.

    Fail-fast: any non-zero exit from ``gh`` or any failure to retrieve
    the secret raises ``StartupAuthError`` and the worker exits.
    """
    secret_name = settings.github_pat_secret_name
    if not secret_name:
        raise StartupAuthError(
            "repo_mode='github' requires GITHUB_PAT_SECRET_NAME to be set"
        )
    logger.info(
        "fetching GitHub PAT from Secrets Manager: secret=%s", secret_name,
    )
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
        # ``input=`` routes the PAT through stdin — the supported channel
        # for ``gh auth login --with-token``. The PAT never appears in
        # argv (which would land in /proc/<pid>/cmdline) and is not put
        # into the environment (which would propagate to every child
        # process the worker spawns).
        result = subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=pat.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            raise StartupAuthError(
                f"`gh auth login --with-token` exited {result.returncode}: "
                f"{result.stderr.decode(errors='replace')}"
            )
        # Install the credential helper so plain ``git clone
        # https://github.com/...`` URLs route through ``gh``.
        result = subprocess.run(
            ["gh", "auth", "setup-git"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise StartupAuthError(
                f"`gh auth setup-git` exited {result.returncode}: "
                f"{result.stderr.decode(errors='replace')}"
            )
    finally:
        # Drop the PAT reference immediately. ``gh`` has it in its
        # keyring now; the worker process must not.
        pat = None  # noqa: F841 - intentional dereference

    logger.info("gh auth bootstrap complete")
