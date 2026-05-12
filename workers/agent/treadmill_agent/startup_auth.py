"""GitHub PAT bootstrap for the worker (Week 4 D.3).

When ``REPO_MODE=github``, the worker authenticates against GitHub via
``gh``'s keyring rather than tokens-in-URLs or ``GH_TOKEN`` envvars.
The PAT itself comes from AWS Secrets Manager — never from an env var
the operator has to set on the host, never from a file mounted into
the container.

The flow at worker boot is:

  1. (Optional) Resolve the worker's AWS credentials from a Secrets
     Manager secret holding ``{"aws_access_key_id":..., "aws_secret_access_key":...}``
     keyed by ``WORKER_AWS_CREDENTIALS_SECRET_NAME`` (ADR-0016 Q16.c).
     This step uses a *bootstrap* boto3 session that picks up whatever
     credentials are already on the host (env / profile / instance role)
     — those credentials only need ``secretsmanager:GetSecretValue`` on
     this one secret. We then build a *worker* session from the fetched
     keys and use it for every subsequent AWS call.

     The chicken-and-egg ("you need credentials to fetch credentials")
     resolves cleanly: bootstrap = whatever's on the host, worker =
     what's in the secret. In the dev-local topology the local-adapter
     usually injects the IAM-User keys as env vars before the worker
     container starts (then this function sees no secret name set and
     just returns the default session) — but in the fully-remote
     topology the worker may run with an instance role that has
     read-access only to its own credential secret, in which case this
     branch fires.

  2. Fetch the GitHub PAT from Secrets Manager keyed by
     ``github_pat_secret_name``. The PAT lives as a local variable in
     this function for the duration of two subprocess calls and is
     dereferenced immediately after.

  3. Pipe the PAT into ``gh auth login --with-token`` via stdin (the
     supported channel; the PAT lives in the kernel pipe buffer for
     the duration of the call). Then run ``gh auth setup-git`` so
     plain ``git clone https://github.com/...`` URLs route through
     ``gh``'s credential helper.

Any failure in any step raises — the worker exits fail-fast rather
than continuing silently and 404-ing later.
"""

from __future__ import annotations

import json
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
    """Return the boto3 session the worker should use for all AWS calls.

    When ``settings.worker_aws_credentials_secret_name`` is unset, we
    return ``boto3.Session(region_name=...)`` — boto3's default
    credential chain (env vars / shared profile / instance role) takes
    over from there.

    When it is set, we build a *bootstrap* session (also via the default
    chain), fetch the credentials secret with it, then build the
    *worker* session from the fetched keys. The bootstrap session goes
    out of scope at function return; the fetched secret string is held
    only as long as needed to parse it.
    """
    region = settings.aws_region
    secret_name = settings.worker_aws_credentials_secret_name
    if not secret_name:
        logger.info(
            "no WORKER_AWS_CREDENTIALS_SECRET_NAME; using default credential chain"
        )
        return boto3.Session(region_name=region)

    logger.info(
        "fetching worker AWS credentials from Secrets Manager: secret=%s region=%s",
        secret_name, region,
    )
    # Bootstrap session uses whatever the operator's default chain
    # provides (env / profile / instance role). It only needs
    # ``secretsmanager:GetSecretValue`` on this single secret.
    bootstrap_session = boto3.Session(region_name=region)
    bootstrap_secrets = bootstrap_session.client("secretsmanager")
    try:
        resp = bootstrap_secrets.get_secret_value(SecretId=secret_name)
    except Exception as exc:  # noqa: BLE001 - surface every failure as a clear error
        raise StartupAuthError(
            f"failed to fetch worker AWS credentials secret {secret_name!r}: {exc}"
        ) from exc
    raw = resp.get("SecretString")
    if not raw:
        raise StartupAuthError(
            f"worker AWS credentials secret {secret_name!r} has no SecretString",
        )
    try:
        creds = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StartupAuthError(
            f"worker AWS credentials secret {secret_name!r} is not valid JSON",
        ) from exc
    access_key = creds.get("aws_access_key_id")
    secret_key = creds.get("aws_secret_access_key")
    if not access_key or not secret_key:
        raise StartupAuthError(
            f"worker AWS credentials secret {secret_name!r} is missing "
            "aws_access_key_id / aws_secret_access_key",
        )
    return boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=creds.get("aws_session_token"),
        region_name=region,
    )


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
