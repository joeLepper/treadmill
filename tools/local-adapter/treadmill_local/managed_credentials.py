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

import boto3


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


def _parse_credentials_file(path: Path) -> dict[str, str]:
    """Parse the credentials file and return the raw data dict.

    Raises ``ManagedCredentialsFileError`` on any read/parse failure or missing
    required keys.  Callers check ``path.exists()`` before calling this.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedCredentialsFileError(
            f"managed-host-credentials at {path} unreadable or malformed: {exc}"
        ) from exc
    if "access_key_id" not in data or "secret_access_key" not in data:
        missing = [k for k in ("access_key_id", "secret_access_key") if k not in data]
        raise ManagedCredentialsFileError(
            f"managed-host-credentials at {path} missing required key(s): {missing}"
        )
    return data


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
    data = _parse_credentials_file(path)
    env: dict[str, str] = {
        "AWS_ACCESS_KEY_ID": data["access_key_id"],
        "AWS_SECRET_ACCESS_KEY": data["secret_access_key"],
    }
    if (tok := data.get("session_token")) is not None:
        env["AWS_SESSION_TOKEN"] = tok
    return env


def resolve_boto3_session(
    profile: str,
    region: str,
    path: Path = MANAGED_HOST_CREDENTIALS_PATH,
) -> boto3.Session:
    """Return a boto3.Session backed by managed-host creds or the SSO profile.

    When ``~/.treadmill/managed-host-credentials.json`` (or ``path``) is absent,
    returns ``boto3.Session(profile_name=profile, region_name=region)`` — the
    unchanged SSO path.  When the file is present, returns a key-material session
    so ``treadmill-local up`` never needs ``aws sso login``.

    Raises ``ManagedCredentialsFileError`` when the file exists but is malformed.
    """
    if not path.exists():
        return boto3.Session(profile_name=profile, region_name=region)
    data = _parse_credentials_file(path)
    kwargs: dict[str, str] = {
        "aws_access_key_id": data["access_key_id"],
        "aws_secret_access_key": data["secret_access_key"],
        "region_name": region,
    }
    if tok := data.get("session_token"):
        kwargs["aws_session_token"] = tok
    return boto3.Session(**kwargs)
