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
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api import repo_profile as repo_profile_mod
from treadmill_api.dependencies_db import get_session
from treadmill_api.models.onboarding import WorkerDeps
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
    git_author_name: str | None = None
    """Per-repo git author name override (ADR-0076). ``None`` defers to the
    deployment default. Must be paired with ``git_author_email``."""
    git_author_email: str | None = None
    """Per-repo git author email override (ADR-0076). ``None`` defers to the
    deployment default. Must be paired with ``git_author_name``."""
    commit_trailer: str | None = None
    """Per-repo commit trailer override (ADR-0076). ``None`` uses the default
    trailer, empty string suppresses it, any other value is used verbatim."""
    worker_deps: WorkerDeps | None = None
    """ADR-0059 per-repo worker extras. ``None`` (omitted) is treated as
    an empty :class:`WorkerDeps`; clients that want "no extras" can omit
    the field rather than serializing an empty object."""
    worker_hints_enabled: bool | None = None
    """Per ADR-0081: control whether the worker's operator_note hint channel
    is enabled for this repo. ``None`` (omitted) defers to the default (true);
    pass ``true`` or ``false`` explicitly to set the flag."""

    @model_validator(mode="after")
    def _validate_git_author_paired(self) -> OnboardRepoRequest:
        """Enforce that git_author_name and git_author_email are paired."""
        name_set = self.git_author_name is not None
        email_set = self.git_author_email is not None
        if name_set != email_set:
            raise ValueError(
                "git_author_name and git_author_email must both be set or both be null"
            )
        return self


class OnboardRepoResponse(BaseModel):
    repo: str
    mode: str
    auto_merge_blocked: bool
    claude_account: str | None = None
    git_author_name: str | None = None
    git_author_email: str | None = None
    commit_trailer: str | None = None
    worker_deps: WorkerDeps = Field(default_factory=WorkerDeps)
    is_public: bool = False
    sensitive_strings: list[str] | None = None
    worker_hints_enabled: bool = True


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

    worker_deps = body.worker_deps or WorkerDeps()
    config = RepoConfig(
        repo=body.repo,
        mode=mode,
        auto_merge_blocked=body.auto_merge_blocked,
        test_command=profile.test_command,
        lint_command=profile.lint_command,
        claude_account=body.claude_account,
        git_author_name=body.git_author_name,
        git_author_email=body.git_author_email,
        commit_trailer=body.commit_trailer,
        worker_deps=worker_deps,
        worker_hints_enabled=body.worker_hints_enabled if body.worker_hints_enabled is not None else True,
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
        git_author_name=body.git_author_name,
        git_author_email=body.git_author_email,
        commit_trailer=body.commit_trailer,
        worker_deps=worker_deps,
        is_public=config.is_public,
        sensitive_strings=config.sensitive_strings,
        worker_hints_enabled=config.worker_hints_enabled,
    )


class RepoConfigResponse(BaseModel):
    repo: str
    mode: str
    auto_merge_blocked: bool
    test_command: str | None = None
    lint_command: str | None = None
    claude_account: str | None = None
    git_author_name: str | None = None
    git_author_email: str | None = None
    commit_trailer: str | None = None
    worker_deps: WorkerDeps = Field(default_factory=WorkerDeps)
    is_public: bool = False
    sensitive_strings: list[str] | None = None
    worker_hints_enabled: bool = True


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
        git_author_name=config.git_author_name,
        git_author_email=config.git_author_email,
        commit_trailer=config.commit_trailer,
        worker_deps=config.worker_deps or WorkerDeps(),
        is_public=config.is_public,
        sensitive_strings=config.sensitive_strings,
        worker_hints_enabled=config.worker_hints_enabled,
    )
