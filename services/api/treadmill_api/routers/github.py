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

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from treadmill_api import github_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github", tags=["github"])


class InstallationTokenRequest(BaseModel):
    repo: str | None = None
    """``owner/name``. Omit to use the App owner's home installation (the
    worker's startup mint, before it knows the task's repo)."""


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

    With ``repo``, resolves that repo's installation. Without it, defaults to
    the App owner's home installation (the earliest-created = lowest id) so the
    worker's startup mint succeeds even on a multi-installation deployment; 503
    when the App is not configured or has no installations.

    HOTFIX (2026-05-21): the no-repo default exists because the worker mints at
    startup *before* it knows the task's repo. The proper fix is per-repo worker
    auth (mint scoped to the task repo). A token for a NON-home repo still
    requires passing ``repo``.
    """
    settings = request.app.state.settings
    if not (settings.github_app_id and settings.github_app_private_key):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub App not configured (GITHUB_APP_ID / private key unset)",
        )
    # Preferred path: mint through the long-lived InstallationTokenCache wired
    # in the app lifespan. It caches ~1h tokens (refresh-before-expiry) and
    # serializes concurrent mints, so the fleet's busiest GitHub call collapses
    # to ~one real mint per installation per refresh window — and the bare
    # github_app calls underneath retry transient 5xx/429 with backoff. Together
    # these fix the 2026-06-04 intermittent-502 wedge. The raw path below is the
    # fallback for callers without a lifespan-wired cache (e.g. unit tests).
    cache = getattr(request.app.state, "installation_token_cache", None)
    if cache is not None:
        # DIAGNOSTIC (2026-06-04): the intermittent-502 has been invariant to
        # caching/retry fixes; log the exact failure shape (cache type, repo,
        # GitHub status + body) to end the speculation. Remove once root-caused.
        try:
            if body.repo:
                installation_id = await cache.installation_id_for(body.repo)
            else:
                installation_id = await cache.home_installation_id()
            tok = await cache.installation_token(installation_id)
        except LookupError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc),
            ) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "DIAG installation-token: cache-path GitHub %s repo=%s "
                "cache=%s url=%s body=%r",
                exc.response.status_code, body.repo, type(cache).__name__,
                str(exc.request.url), (exc.response.text or "")[:200],
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=f"GitHub API error minting token: {exc.response.status_code}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 — diagnostic catch-all, re-raises
            logger.warning(
                "DIAG installation-token: cache-path UNEXPECTED %s repo=%s "
                "cache=%s: %s",
                type(exc).__name__, body.repo, type(cache).__name__,
                str(exc)[:200],
            )
            raise
        return InstallationTokenResponse(
            token=tok.token,
            expires_at=tok.expires_at.isoformat(),
            installation_id=installation_id,
            repo=body.repo,
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
                if not ids:
                    raise HTTPException(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="GitHub App has no installations",
                    )
                # No repo supplied — default to the App owner's home
                # installation (earliest-created = lowest id) instead of 400.
                installation_id = min(ids)
                if len(ids) > 1:
                    logger.info(
                        "installation-token: no repo; defaulting to home "
                        "installation %s (of %d installations)",
                        installation_id, len(ids),
                    )
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
