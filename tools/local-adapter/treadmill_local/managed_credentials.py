"""Managed-host IAM credentials resolver (ADR-0072).

Reads ``~/.treadmill/managed-host-credentials.json`` and returns an env-overlay
dict with ``AWS_*`` keys.  Returns ``None`` when the file is absent so callers
fall back to operator SSO (``AWS_PROFILE``).  Raises ``ManagedCredentialsFileError``
when the file exists but cannot be read or parsed — loud failure, never silent
fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict


class ManagedHostCredentials(TypedDict, total=False):
    access_key_id: str
    secret_access_key: str
    session_token: str  # optional, federation / assumed-role
    expires_at: str | None  # optional, ISO-8601, informational only


MANAGED_HOST_CREDENTIALS_PATH = (
    Path.home() / ".treadmill/managed-host-credentials.json"
)


class ManagedCredentialsFileError(Exception):
    """File present but unreadable or malformed — operator must fix it."""


def resolve_managed_host_credentials(
    path: Path = MANAGED_HOST_CREDENTIALS_PATH,
) -> dict[str, str] | None:
    """Read managed-host credentials and return an env-overlay dict or None.

    Returns:
      - ``None`` when the file is absent (caller falls back to SSO).
      - ``dict[str, str]`` with ``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY``
        (and ``AWS_SESSION_TOKEN`` when present) when the file exists and parses.

    Raises:
      ``ManagedCredentialsFileError`` when the file exists but is unreadable,
      contains malformed JSON, or is missing the required two fields.  Never
      silently falls back — a half-broken file is operator error.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedCredentialsFileError(
            f"managed-host-credentials at {path} unreadable or malformed: {exc}"
        ) from exc
    try:
        env: dict[str, str] = {
            "AWS_ACCESS_KEY_ID": data["access_key_id"],
            "AWS_SECRET_ACCESS_KEY": data["secret_access_key"],
        }
    except KeyError as exc:
        raise ManagedCredentialsFileError(
            f"managed-host-credentials at {path} missing required key: {exc}"
        ) from exc
    if (tok := data.get("session_token")) is not None:
        env["AWS_SESSION_TOKEN"] = tok
    return env
