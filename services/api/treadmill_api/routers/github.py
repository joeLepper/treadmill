"""GitHub App endpoints (ADR-0049) — mint short-lived installation tokens.

The worker calls ``POST /api/v1/github/installation-token`` at startup to get a
GitHub token for ``gh``/git **without ever holding the App private key** (which
stays on the API). This replaces the worker's PAT fetch (phase 5), and lets the
personal PAT be decommissioned (phase 8).

Internal endpoint: in dev_local / fully_remote the API is reachable only on the
internal network (the public surface is the webhook API Gateway). Adding caller
auth is a follow-up for fully_remote hardening.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from treadmill_api import github_app

router = APIRouter(prefix="/api/v1/github", tags=["github"])


class InstallationTokenRequest(BaseModel):
    repo: str | None = None
    """``owner/name``. Omit to use the sole installation (dev-local case)."""


class InstallationTokenResponse(BaseModel):
    token: str
    expires_at: str
    installation_id: int
    repo: str | None = None


@router.post("/installation-token", response_model=InstallationTokenResponse)
async def mint_installation_token(
    body: InstallationTokenRequest, request: Request,
) -> InstallationTokenResponse:
    """Mint a short-lived installation access token.

    With ``repo``, resolves that repo's installation. Without it, uses the sole
    installation (400 if there are zero or several — multi-org callers must pass
    a repo). 503 when the App is not configured.
    """
    settings = request.app.state.settings
    if not (settings.github_app_id and settings.github_app_private_key):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub App not configured (GITHUB_APP_ID / private key unset)",
        )
    app_id = settings.github_app_id
    pk = settings.github_app_private_key

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            if body.repo:
                installation_id = await github_app.resolve_installation_id(
                    client, app_id=app_id, private_key_pem=pk, repo=body.repo,
                )
            else:
                ids = await github_app.list_installation_ids(
                    client, app_id=app_id, private_key_pem=pk,
                )
                if len(ids) != 1:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"repo required: {len(ids)} installations exist "
                            "(cannot disambiguate)"
                        ),
                    )
                installation_id = ids[0]
            tok = await github_app.fetch_installation_token(
                client, app_id=app_id, private_key_pem=pk,
                installation_id=installation_id,
            )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=f"GitHub API error minting token: {exc.response.status_code}",
            ) from exc

    return InstallationTokenResponse(
        token=tok.token,
        expires_at=tok.expires_at.isoformat(),
        installation_id=installation_id,
        repo=body.repo,
    )
