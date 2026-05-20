"""GitHub auth provider — PAT or App installation token, per config (ADR-0049).

A thin abstraction over "give me a GitHub token for this repo." Selects
between the legacy single PAT and the GitHub App's per-installation tokens
based on whether ``settings.github_app_id`` and
``settings.github_app_private_key`` are both set. Lets call sites stay
indifferent to which mode is active while the migration is in flight.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from treadmill_api.config import Settings
from treadmill_api.github_app import InstallationTokenCache


class GitHubAuthProvider(Protocol):
    """Yields a GitHub token usable for API/git operations against ``repo``."""

    async def token_for_repo(self, repo: str) -> str: ...


class _PATAuthProvider:
    """Legacy path: returns the single configured PAT for every repo."""

    def __init__(self, token: str | None) -> None:
        self._token = token

    async def token_for_repo(self, repo: str) -> str:
        # Preserve the legacy contract: callers got ``settings.github_token``
        # directly before this abstraction existed, including its None-ness.
        return self._token  # type: ignore[return-value]


class _AppAuthProvider:
    """App path: delegates to an ``InstallationTokenCache``."""

    def __init__(self, cache: InstallationTokenCache) -> None:
        self._cache = cache

    async def token_for_repo(self, repo: str) -> str:
        return await self._cache.token_for_repo(repo)


def build_github_auth_provider(
    settings: Settings, http_client: httpx.AsyncClient,
) -> GitHubAuthProvider:
    """Choose the App path when both id + private key are set; else PAT."""
    if settings.github_app_id and settings.github_app_private_key:
        cache = InstallationTokenCache(
            http_client,
            app_id=settings.github_app_id,
            private_key_pem=settings.github_app_private_key,
        )
        return _AppAuthProvider(cache)
    return _PATAuthProvider(settings.github_token)
