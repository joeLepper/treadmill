"""Worker configuration loaded from environment variables.

The autoscaler launches workers as one-shot containers — by default
they read env once at startup, process exactly one message, and exit.
Set ``EXIT_AFTER_STEP=false`` (e.g. for long-poll dev sessions) to keep
the runner looping.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_url: str
    work_queue_url: str
    events_topic_arn: str | None
    aws_endpoint_url: str | None
    aws_region: str
    repo_mode: str
    """``local`` and ``github`` are the supported modes. ``local`` clones
    from a file-backed bare repo; ``github`` clones over HTTPS with a
    PAT-backed ``gh`` credential helper (the PAT itself is fetched from
    Secrets Manager at startup; see ``github_pat_secret_name``).
    Anything else raises in ``git.clone``."""

    bare_repos_dir: str
    """Host path mounted into the worker for local-mode bare repos.
    Bare repos live at ``<dir>/<owner>__<name>.git``."""

    workspace_dir: str
    """Where the worker materializes per-step working trees."""

    exit_after_step: bool
    """If true (default), exit after processing a single step. Matches
    the autoscaler's one-shot mode (max_capacity=1, EXACT_CAPACITY) —
    new replicas are spawned for each subsequent message. Flip to
    ``false`` to keep one worker long-polling indefinitely (dev only).
    """

    poll_wait_seconds: int
    """Long-poll wait window. SQS max is 20."""

    claude_credentials_path: str
    """Path inside the container to the user's Claude OAuth credentials.
    The local-adapter mounts ``~/.claude/.credentials.json`` here so
    Claude Code can refresh on the user's subscription."""

    github_pat_secret_name: str | None = None
    """Secrets Manager secret name (NOT ARN — Secrets Manager's lookup
    resolves a name to its ARN automatically; the YAML schema in
    ADR-0016 standardizes on ``secrets.github_pat_secret_name``). Set
    in ``dev_local`` / ``fully_remote`` modes when ``repo_mode='github'``
    so the worker can fetch the PAT from Secrets Manager at startup
    and hand it to ``gh auth login --with-token``. Unset in fully-local
    mode (where ``repo_mode='local'`` is the only path). There is no
    direct ``GITHUB_PAT`` env-var setting: PATs only enter the worker
    via Secrets Manager.

    Note (ADR-0019): the worker's *AWS* credentials are no longer
    fetched from Secrets Manager by the worker itself — the local-adapter
    injects them as env vars at container-spawn time. The PAT fetch
    above is still the worker's responsibility because that's how
    ``gh`` ends up with the token in its keyring without the value
    crossing the host/container env-var boundary."""


_TRUE_VALUES = {"true", "1", "yes"}
_FALSE_VALUES = {"false", "0", "no"}


def _parse_bool(raw: str | None, *, default: bool, var_name: str) -> bool:
    """Parse a boolean env var with strict, case-insensitive matching.

    Accepts ``true/false/1/0/yes/no`` (case-insensitive). Anything else
    (including empty) raises so a typo doesn't silently flip behavior.
    ``None`` (env var unset) returns ``default``.
    """
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ValueError(
        f"invalid boolean for {var_name}: {raw!r}; "
        f"accepted: true/false/1/0/yes/no (case-insensitive)"
    )


def load() -> Settings:
    return Settings(
        api_url=os.environ.get("TREADMILL_API_URL", "http://treadmill-api:8088"),
        work_queue_url=_required("WORK_QUEUE_URL"),
        events_topic_arn=os.environ.get("EVENTS_TOPIC_ARN"),
        aws_endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
        aws_region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        repo_mode=os.environ.get("REPO_MODE", "local"),
        bare_repos_dir=os.environ.get("BARE_REPOS_DIR", "/var/treadmill/repos"),
        workspace_dir=os.environ.get("WORKSPACE_DIR", "/var/treadmill/workspaces"),
        exit_after_step=_parse_bool(
            os.environ.get("EXIT_AFTER_STEP"),
            default=True, var_name="EXIT_AFTER_STEP",
        ),
        poll_wait_seconds=int(os.environ.get("POLL_WAIT_SECONDS", "20")),
        claude_credentials_path=os.environ.get(
            "CLAUDE_CREDENTIALS_PATH",
            "/root/.claude/.credentials.json",
        ),
        github_pat_secret_name=os.environ.get("GITHUB_PAT_SECRET_NAME"),
    )


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"required env var {name} is unset")
    return val
