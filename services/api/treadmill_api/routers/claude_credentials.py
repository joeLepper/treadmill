"""Claude credential resolver — ADR-0055.

Per-repo routing of Claude account credentials, mirroring the GitHub App
``installation-token`` endpoint shape in ``routers/github.py``. The worker
calls this at its per-step re-mint seam (after ``ctx.repo`` is known) and
uses the returned token to set ``CLAUDE_CODE_OAUTH_TOKEN`` or
``ANTHROPIC_API_KEY`` in the Claude Code subprocess env.

Resolution: repo → ``RepoConfig.claude_account`` (or
``CLAUDE_DEFAULT_ACCOUNT`` when null) → ``claude_accounts_json[name]`` →
``SecretsManager.GetSecretValue(secret_name)``. Failure modes are
explicit and **never silently fall back across accounts**:

  * 503 — feature unconfigured (no ``CLAUDE_ACCOUNTS_JSON``, malformed JSON,
    or neither repo-level nor default account is set).
  * 404 — resolved account name is not in the configured map.
  * 502 — Secrets Manager fetch failed or returned no string value.

Optional fallback (ADR-0066): when ``RepoConfig.claude_account_fallback``
is set, the response also carries a ``fallback`` credential resolved
best-effort. Any failure on the fallback side logs a warning and leaves
``fallback=None``; it never turns a working primary into an error.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal

import boto3
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.onboarding_store import OnboardingStore


router = APIRouter(prefix="/api/v1/claude", tags=["claude-credentials"])

_log = logging.getLogger(__name__)


class ClaudeAccountConfig(BaseModel):
    """Shape of a single entry in ``claude_accounts_json``."""

    type: Literal["oauth", "api_key"]
    secret_name: str = Field(..., min_length=1)


class ClaudeCredentialsRequest(BaseModel):
    repo: str = Field(..., min_length=1)


class ClaudeFallbackCredential(BaseModel):
    account: str
    type: Literal["oauth", "api_key"]
    token: str


class ClaudeCredentialsResponse(BaseModel):
    repo: str
    account: str
    type: Literal["oauth", "api_key"]
    token: str
    fallback: ClaudeFallbackCredential | None = None


def _parse_accounts(raw: str | None) -> dict[str, ClaudeAccountConfig]:
    """Parse ``CLAUDE_ACCOUNTS_JSON`` into a typed map; HTTPException on garbage."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"CLAUDE_ACCOUNTS_JSON is not valid JSON: {exc}",
        )
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLAUDE_ACCOUNTS_JSON must be a JSON object",
        )
    out: dict[str, ClaudeAccountConfig] = {}
    for name, cfg in data.items():
        try:
            out[name] = ClaudeAccountConfig.model_validate(cfg)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"claude_accounts[{name!r}] is invalid: {exc}",
            )
    return out


def _make_secrets_client(region: str):
    """Boto factory; monkeypatched in tests to return a fake."""
    return boto3.client("secretsmanager", region_name=region)


@router.post("/credentials", response_model=ClaudeCredentialsResponse)
async def fetch_claude_credentials(
    body: ClaudeCredentialsRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ClaudeCredentialsResponse:
    settings = request.app.state.settings
    accounts = _parse_accounts(getattr(settings, "claude_accounts_json", None))
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No Claude accounts configured "
                "(CLAUDE_ACCOUNTS_JSON unset or empty)."
            ),
        )

    cfg = await OnboardingStore().get_repo_config(session, body.repo)
    repo_account = cfg.claude_account if cfg is not None else None
    account_name = repo_account or getattr(settings, "claude_default_account", None)
    if not account_name:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"No claude_account for repo {body.repo!r} and no "
                "CLAUDE_DEFAULT_ACCOUNT configured."
            ),
        )

    account = accounts.get(account_name)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"claude_account {account_name!r} not in configured accounts: "
                f"{sorted(accounts)}"
            ),
        )

    sm = _make_secrets_client(settings.aws_region)
    try:
        secret = sm.get_secret_value(SecretId=account.secret_name)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Secrets Manager fetch failed for account "
                f"{account_name!r}: {type(exc).__name__}"
            ),
        )
    token = secret.get("SecretString")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Secret for account {account_name!r} has no SecretString."
            ),
        )

    fallback: ClaudeFallbackCredential | None = None
    fallback_name = cfg.claude_account_fallback if cfg is not None else None
    if fallback_name:
        fallback_account = accounts.get(fallback_name)
        if fallback_account is None:
            _log.warning(
                "claude_account_fallback %r not in accounts map %s — skipping fallback",
                fallback_name, sorted(accounts),
            )
        else:
            try:
                fallback_secret = sm.get_secret_value(
                    SecretId=fallback_account.secret_name
                )
                fallback_token = fallback_secret.get("SecretString")
                if not fallback_token:
                    _log.warning(
                        "Secret for fallback account %r has no SecretString"
                        " — skipping fallback",
                        fallback_name,
                    )
                else:
                    fallback = ClaudeFallbackCredential(
                        account=fallback_name,
                        type=fallback_account.type,
                        token=fallback_token,
                    )
            except Exception as exc:
                _log.warning(
                    "Secrets Manager fetch failed for fallback account %r: %s"
                    " — skipping fallback",
                    fallback_name, type(exc).__name__,
                )

    return ClaudeCredentialsResponse(
        repo=body.repo,
        account=account_name,
        type=account.type,
        token=token,
        fallback=fallback,
    )
