"""GitHub auth provider — PAT or App installation token, per config (ADR-0049).

A thin abstraction over "give me a GitHub token for this repo." Selects
between the legacy single PAT and the GitHub App's per-installation tokens
based on whether ``settings.github_app_id`` and
``settings.github_app_private_key`` are both set. Lets call sites stay
indifferent to which mode is active while the migration is in flight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from treadmill_api.config import Settings
from treadmill_api.github_app import InstallationTokenCache

logger = logging.getLogger("treadmill.github_auth")

_GITHUB_API_BASE = "https://api.github.com"
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


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


# ── API client construction (ADR-0049 cutover) ───────────────────────────────


def extract_repo_from_github_path(path: str) -> str | None:
    """Extract ``owner/name`` from a ``/repos/{owner}/{name}/...`` API path.

    Returns ``None`` for non-repo paths (every current API call site is
    repo-scoped, so those are the only ones needing a per-repo token).
    """
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    return None


class _InstallationAuthHook:
    """httpx request hook: stamp a per-repo installation-token Authorization.

    Reads the repo from the request's ``/repos/{owner}/{name}/...`` path and
    mints/sets ``Authorization: Bearer <installation token>``. Non-repo
    requests are left as-is.
    """

    def __init__(self, provider: GitHubAuthProvider) -> None:
        self._provider = provider

    async def __call__(self, request: httpx.Request) -> None:
        repo = extract_repo_from_github_path(request.url.path)
        if repo is None:
            return
        token = await self._provider.token_for_repo(repo)
        request.headers["Authorization"] = f"Bearer {token}"


@dataclass
class GitHubClients:
    """The GitHub client all call sites use, plus auxiliary clients to close.

    ``client`` is ``None`` when no auth is configured (callers short-circuit).
    ``aclose`` closes it and any minting client created for the App path.
    """

    client: httpx.AsyncClient | None
    _aux: list[httpx.AsyncClient] = field(default_factory=list)

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
        for aux in self._aux:
            await aux.aclose()


def build_github_clients(settings: Settings) -> GitHubClients:
    """Build the API's GitHub client per ADR-0049 (guarded cutover).

    - App path (``github_app_id`` + ``github_app_private_key`` set): a
      hook-authenticated client that stamps a per-repo installation token on
      each request, plus a dedicated minting client (App-JWT calls must not go
      through the hook).
    - PAT path: the legacy static-Bearer client — byte-for-byte the prior
      behavior, so nothing changes until the App is configured.
    - Neither: ``client=None``.
    """
    if settings.github_app_id and settings.github_app_private_key:
        minting = httpx.AsyncClient(timeout=10.0)
        provider = build_github_auth_provider(settings, minting)
        client = httpx.AsyncClient(
            base_url=_GITHUB_API_BASE,
            headers=dict(_GITHUB_HEADERS),
            event_hooks={"request": [_InstallationAuthHook(provider)]},
            timeout=10.0,
        )
        logger.info(
            "GitHub auth: App path active (app_id=%s) — per-repo installation tokens",
            settings.github_app_id,
        )
        return GitHubClients(client=client, _aux=[minting])
    if settings.github_token:
        client = httpx.AsyncClient(
            base_url=_GITHUB_API_BASE,
            headers={
                **_GITHUB_HEADERS,
                "Authorization": f"Bearer {settings.github_token}",
            },
            timeout=10.0,
        )
        return GitHubClients(client=client, _aux=[])
    return GitHubClients(client=None)
