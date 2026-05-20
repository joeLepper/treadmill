"""GitHub App identity — mint short-lived per-installation tokens (ADR-0049).

Replaces the single personal PAT. The App's id + RS256 private key sign a
short-lived App JWT; that JWT is exchanged for an installation access token
(~1h TTL) scoped to one installation. Tokens are cached per installation and
refreshed before expiry; repo→installation resolution is cached too. See
ADR-0049 for the auth-flow sequence diagram.

RS256 signing is done directly with ``cryptography`` (already a dependency) to
avoid pulling in PyJWT for the handful of claims we need.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

_GITHUB_API = "https://api.github.com"
_GITHUB_ACCEPT = "application/vnd.github+json"

# App JWT max lifetime is 10 minutes; use 9 to leave clock-skew headroom.
_JWT_TTL_SECONDS = 540
# GitHub recommends backdating ``iat`` 60s to tolerate clock drift.
_JWT_BACKDATE_SECONDS = 60
# Refresh an installation token when within this margin of expiry, so a long
# worker run never presents a token that expires mid-operation.
_TOKEN_REFRESH_MARGIN_SECONDS = 300


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str:
    """Return a signed RS256 App JWT (``iss=app_id``, ~9-minute lifetime).

    ``now`` (unix seconds) is injectable for tests; defaults to wall clock.
    """
    issued = int(time.time()) if now is None else now
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": issued - _JWT_BACKDATE_SECONDS,
        "exp": issued + _JWT_TTL_SECONDS,
        "iss": str(app_id),
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    ).encode("ascii")
    key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None,
    )
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + _b64url(signature)


@dataclass
class InstallationToken:
    """A minted installation access token and its expiry."""

    token: str
    expires_at: datetime

    def is_fresh(self, *, margin_seconds: int = _TOKEN_REFRESH_MARGIN_SECONDS) -> bool:
        """True when the token has more than ``margin_seconds`` of life left."""
        remaining = (self.expires_at - datetime.now(timezone.utc)).total_seconds()
        return remaining > margin_seconds


def _app_jwt_headers(app_id: str, private_key_pem: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {build_app_jwt(app_id, private_key_pem)}",
        "Accept": _GITHUB_ACCEPT,
    }


async def resolve_installation_id(
    client: httpx.AsyncClient,
    *,
    app_id: str,
    private_key_pem: str,
    repo: str,
) -> int:
    """Resolve a ``owner/name`` repo to its App installation id.

    ``GET /repos/{repo}/installation`` authenticated with the App JWT.
    """
    resp = await client.get(
        f"{_GITHUB_API}/repos/{repo}/installation",
        headers=_app_jwt_headers(app_id, private_key_pem),
    )
    resp.raise_for_status()
    return int(resp.json()["id"])


async def fetch_installation_token(
    client: httpx.AsyncClient,
    *,
    app_id: str,
    private_key_pem: str,
    installation_id: int,
) -> InstallationToken:
    """Mint a fresh installation access token.

    ``POST /app/installations/{id}/access_tokens`` authenticated with the App
    JWT. The response carries ``token`` and an ISO-8601 ``expires_at``.
    """
    resp = await client.post(
        f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers=_app_jwt_headers(app_id, private_key_pem),
    )
    resp.raise_for_status()
    data = resp.json()
    expires_at = datetime.fromisoformat(
        data["expires_at"].replace("Z", "+00:00"),
    )
    return InstallationToken(token=data["token"], expires_at=expires_at)


class InstallationTokenCache:
    """Per-installation token cache with refresh-before-expiry.

    Also memoizes repo→installation_id resolution. An ``asyncio.Lock``
    serializes minting so concurrent callers for the same installation don't
    issue duplicate token requests (GitHub returns a fresh token each call).
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        app_id: str,
        private_key_pem: str,
    ) -> None:
        self._client = client
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._tokens: dict[int, InstallationToken] = {}
        self._repo_installations: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def installation_id_for(self, repo: str) -> int:
        cached = self._repo_installations.get(repo)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._repo_installations.get(repo)
            if cached is not None:
                return cached
            installation_id = await resolve_installation_id(
                self._client,
                app_id=self._app_id,
                private_key_pem=self._private_key_pem,
                repo=repo,
            )
            self._repo_installations[repo] = installation_id
            return installation_id

    async def token_for_installation(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached is not None and cached.is_fresh():
            return cached.token
        async with self._lock:
            cached = self._tokens.get(installation_id)
            if cached is not None and cached.is_fresh():
                return cached.token
            fresh = await fetch_installation_token(
                self._client,
                app_id=self._app_id,
                private_key_pem=self._private_key_pem,
                installation_id=installation_id,
            )
            self._tokens[installation_id] = fresh
            return fresh.token

    async def token_for_repo(self, repo: str) -> str:
        installation_id = await self.installation_id_for(repo)
        return await self.token_for_installation(installation_id)
