"""Onboarding router — ADR-0051.

Deployment-side onboarding endpoint that registers a repo by persisting
a posted profile + config to the ADR-0050 tables. Builds on
:class:`treadmill_api.onboarding_store.OnboardingStore` plus the
:mod:`treadmill_api.repo_profile` / :mod:`treadmill_api.repo_config`
dataclasses — the router is a thin HTTP surface; the upsert and
recommendation logic live in those modules.

``POST /api/v1/onboarding/repos`` accepts the discovered profile and
(optionally) an explicit mode. When ``mode`` is omitted the handler
calls :func:`repo_profile.recommend_mode` so the discovery side can
defer the decision to the deployment side's view of the persisted
profile.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api import repo_profile as repo_profile_mod
from treadmill_api.dependencies_db import get_session
from treadmill_api.onboarding_store import OnboardingStore
from treadmill_api.repo_config import VALID_MODES, RepoConfig


router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])


class OnboardRepoRequest(BaseModel):
    repo: str = Field(..., min_length=1, max_length=255)
    profile: dict[str, Any]
    """Plain-dict shape of :class:`treadmill_api.repo_profile.RepoProfile`.

    The handler fills in ``profile["repo"]`` from ``repo`` if missing so
    callers don't have to repeat it."""
    mode: str | None = None
    """Optional ``"conform"`` / ``"adapt"`` override. When ``None`` the
    handler runs :func:`recommend_mode` on the parsed profile."""
    auto_merge_blocked: bool = False
    claude_account: str | None = None
    """Named Claude account for workers operating on this repo (ADR-0055).
    ``None`` defers to the deployment's ``claude_default_account``."""


class OnboardRepoResponse(BaseModel):
    repo: str
    mode: str
    auto_merge_blocked: bool
    claude_account: str | None = None


@router.post(
    "/repos",
    response_model=OnboardRepoResponse,
    status_code=status.HTTP_200_OK,
)
async def onboard_repo(
    body: OnboardRepoRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OnboardRepoResponse:
    profile_data = dict(body.profile)
    profile_data.setdefault("repo", body.repo)
    profile = repo_profile_mod.from_dict(profile_data)

    mode = body.mode or repo_profile_mod.recommend_mode(profile)
    if mode not in VALID_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"mode must be one of {sorted(VALID_MODES)}; got {mode!r}",
        )

    config = RepoConfig(
        repo=body.repo,
        mode=mode,
        auto_merge_blocked=body.auto_merge_blocked,
        test_command=profile.test_command,
        lint_command=profile.lint_command,
        claude_account=body.claude_account,
    )

    store = OnboardingStore()
    await store.upsert_repo_profile(session, profile)
    await store.upsert_repo_config(session, config)
    await session.commit()

    return OnboardRepoResponse(
        repo=body.repo,
        mode=mode,
        auto_merge_blocked=body.auto_merge_blocked,
        claude_account=body.claude_account,
    )


class RepoConfigResponse(BaseModel):
    repo: str
    mode: str
    auto_merge_blocked: bool
    test_command: str | None = None
    lint_command: str | None = None
    claude_account: str | None = None


@router.get(
    "/repos/{repo:path}",
    response_model=RepoConfigResponse,
)
async def get_repo(
    repo: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RepoConfigResponse:
    """Return a registered repo's config (mode, auto-merge block, commands).

    The mode-aware authoring skill (ADR-0054 d.4) calls this to route
    adapt (pull→author→push) vs conform (in-repo + commit). 404 when the
    repo was never onboarded.
    """
    config = await OnboardingStore().get_repo_config(session, repo)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"repo {repo!r} is not onboarded",
        )
    return RepoConfigResponse(
        repo=config.repo,
        mode=config.mode,
        auto_merge_blocked=config.auto_merge_blocked,
        test_command=config.test_command,
        lint_command=config.lint_command,
        claude_account=config.claude_account,
    )
