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
import uuid
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api import github_app
from treadmill_api.dependencies_db import get_session
from treadmill_api.eventbus import get_publisher
from treadmill_api.webhooks.normalize import normalize_github_event
from treadmill_api.webhooks.persist import persist_and_resolve_webhook_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github", tags=["github"])

# Namespace for deterministic poll-ingest event ids (ADR-0113).
_POLL_NS = uuid.UUID("6f0b4a2e-0113-4c0d-9c11-7013ea5e0113")


class PollIngestRequest(BaseModel):
    """A PR-state poller's OBSERVED state for a webhookless repo (ADR-0113)."""

    repo: str = Field(min_length=1, max_length=255)
    pr_number: int = Field(ge=1)
    action: Literal["pr_merged"]  # v1 merge leg; the CI leg is a follow-on
    merge_sha: str = Field(min_length=1, max_length=64)


class PollIngestResponse(BaseModel):
    event_id: str
    entity_type: str
    action: str
    already_ingested: bool = False
    """True when this observed state was already recorded — the seam was NOT re-run,
    so the event was NOT re-published (the coordinator does not re-process it). The
    shared seam publishes UNCONDITIONALLY even on an ON-CONFLICT insert, so we cannot
    lean on it for idempotency — we gate on the deterministic event_id's prior
    existence here."""


def _poll_event_id(repo: str, pr_number: int, action: str, anchor: str) -> uuid.UUID:
    """Deterministic event id so a RE-POLL of the same observed state upserts onto the
    SAME events row (`persist_and_resolve_webhook_event`'s ON CONFLICT (id) DO NOTHING)
    — idempotency without a poller-side "already emitted" check."""
    return uuid.uuid5(_POLL_NS, f"pr-poll:{repo}:{pr_number}:{action}:{anchor}")


@router.post("/poll-ingest", response_model=PollIngestResponse)
async def poll_ingest(
    body: PollIngestRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
) -> PollIngestResponse:
    """Ingest a poller's observed PR state as a SYNTHESIZED github event (ADR-0113).

    Routed through the SAME shared webhook seam (`persist_and_resolve_webhook_event`)
    a real delivery uses, so the result is byte-identical: task_id resolved from
    `(repo, pr_number)` via `task_prs`, `events.commit_sha` set by the ADR-0014
    commit-anchor extraction, persisted, and published to the coordinator. A
    deterministic event_id makes a re-poll a no-op. For repos with no App/webhook."""
    if body.action == "pr_merged":
        # Reconstruct the exact `pull_request` closed+merged body the webhook carries,
        # so `normalize_github_event` + the commit-anchor extraction produce identical
        # output. Only the fields the normalizer + `_extract_commit_sha` read matter.
        synthetic = {
            "action": "closed",
            "repository": {"full_name": body.repo},
            "sender": {"login": "pr-poll"},
            "pull_request": {
                "number": body.pr_number,
                "merged": True,
                "merge_commit_sha": body.merge_sha,
                "head": {"sha": body.merge_sha, "ref": ""},
            },
        }
        normalized = normalize_github_event("pull_request", synthetic)
        if normalized is None:  # defensive; a merged-closed PR always normalizes
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="poll-ingest: synthetic body did not normalize to pr_merged",
            )
        event_id = _poll_event_id(
            body.repo, body.pr_number, "pr_merged", body.merge_sha
        )
        # IDEMPOTENCY (Ernie): the shared seam publishes UNCONDITIONALLY, even when its
        # ON-CONFLICT insert is a no-op — so a re-poll would RE-PUBLISH pr_merged and the
        # coordinator would re-process it. Gate on the deterministic event_id's prior
        # existence and skip the seam entirely (no re-publish) when already recorded.
        already = (
            await session.execute(
                text("SELECT 1 FROM events WHERE id = :eid"), {"eid": str(event_id)}
            )
        ).first() is not None
        if already:
            return PollIngestResponse(
                event_id=str(event_id),
                entity_type="github",
                action="pr_merged",
                already_ingested=True,
            )
        event = await persist_and_resolve_webhook_event(
            session,
            normalized,
            synthetic,
            getattr(request.app.state, "redis", None),
            get_publisher(),
            event_id=event_id,
        )
        return PollIngestResponse(
            event_id=str(event.id),
            entity_type=normalized.entity_type,
            action=normalized.action,
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"poll-ingest: unsupported action {body.action!r}",
    )


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
