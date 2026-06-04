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
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger("treadmill.github_app")

_GITHUB_API = "https://api.github.com"
_GITHUB_ACCEPT = "application/vnd.github+json"

# Transient-error retry for GitHub App-JWT calls. GitHub intermittently returns
# 5xx / 429 (incl. secondary rate limits) on the token-mint endpoints under
# fleet load; without a retry the API mapped each one straight to a 502 that
# wedged the calling worker's step (2026-06-04 incident — the
# ``/installation-token`` route was the fleet's single busiest GitHub call and
# the only one with no caching). Bounded exponential backoff, honoring
# ``Retry-After`` when present, plus the token cache below (which removes the
# vast majority of these calls), is the durable fix.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BASE_DELAY_SECONDS = 0.5
_RETRY_MAX_DELAY_SECONDS = 8.0


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    """Backoff for ``attempt`` (0-based). Honors ``Retry-After`` (seconds) when
    the upstream supplied it; otherwise exponential ``base * 2**attempt``,
    capped. Kept tiny + deterministic so tests can drive it without sleeping."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), _RETRY_MAX_DELAY_SECONDS)
    return min(_RETRY_BASE_DELAY_SECONDS * (2 ** attempt), _RETRY_MAX_DELAY_SECONDS)


async def _send_with_retry(
    client: httpx.AsyncClient, method: str, url: str, *, headers: dict[str, str],
) -> httpx.Response:
    """Issue a GitHub request, retrying transient failures with backoff.

    Retries on retryable HTTP statuses (:data:`_RETRYABLE_STATUSES`) and on
    transport errors (connect/read timeouts, resets). Non-retryable statuses
    (401/403-not-rate-limited/404/422) and the final attempt raise immediately,
    preserving the caller's existing ``httpx.HTTPStatusError`` contract — the
    route still maps a genuine GitHub failure to 502.
    """
    last_exc: Exception
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            resp = await client.request(method, url, headers=headers)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if (
                status_code not in _RETRYABLE_STATUSES
                or attempt == _RETRY_MAX_ATTEMPTS - 1
            ):
                raise
            last_exc = exc
            delay = _retry_delay(attempt, exc.response)
        except httpx.TransportError as exc:
            if attempt == _RETRY_MAX_ATTEMPTS - 1:
                raise
            last_exc = exc
            delay = _retry_delay(attempt, None)
        logger.warning(
            "github_app: transient failure on %s %s (%s); retry %d/%d in %.1fs",
            method, url, type(last_exc).__name__,
            attempt + 1, _RETRY_MAX_ATTEMPTS - 1, delay,
        )
        await asyncio.sleep(delay)
    raise last_exc  # unreachable — the loop either returns or raises above

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
    resp = await _send_with_retry(
        client, "GET", f"{_GITHUB_API}/repos/{repo}/installation",
        headers=_app_jwt_headers(app_id, private_key_pem),
    )
    return int(resp.json()["id"])


async def list_installation_ids(
    client: httpx.AsyncClient,
    *,
    app_id: str,
    private_key_pem: str,
) -> list[int]:
    """List the App's installation ids (``GET /app/installations``).

    Used to resolve "the installation" when no specific repo is given — the
    dev-local single-installation case. Multi-installation callers should
    resolve by repo via :func:`resolve_installation_id` instead.
    """
    resp = await _send_with_retry(
        client, "GET", f"{_GITHUB_API}/app/installations",
        headers=_app_jwt_headers(app_id, private_key_pem),
    )
    return [int(item["id"]) for item in resp.json()]


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
    resp = await _send_with_retry(
        client, "POST",
        f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers=_app_jwt_headers(app_id, private_key_pem),
    )
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
        self._home_installation_id: int | None = None
        self._lock = asyncio.Lock()

    async def home_installation_id(self) -> int:
        """The App owner's home installation (lowest id), cached.

        Backs the worker's repo-less startup mint. Raises ``LookupError`` when
        the App has no installations (the route maps that to 503).
        """
        if self._home_installation_id is not None:
            return self._home_installation_id
        async with self._lock:
            if self._home_installation_id is not None:
                return self._home_installation_id
            ids = await list_installation_ids(
                self._client,
                app_id=self._app_id,
                private_key_pem=self._private_key_pem,
            )
            if not ids:
                raise LookupError("GitHub App has no installations")
            self._home_installation_id = min(ids)
            return self._home_installation_id

    async def installation_token(self, installation_id: int) -> InstallationToken:
        """Cached :class:`InstallationToken` (token + expiry) for an
        installation, refreshed within the expiry margin. The object form the
        ``/installation-token`` route needs; :meth:`token_for_installation`
        wraps it for the string-only callers."""
        cached = self._tokens.get(installation_id)
        if cached is not None and cached.is_fresh():
            return cached
        async with self._lock:
            cached = self._tokens.get(installation_id)
            if cached is not None and cached.is_fresh():
                return cached
            fresh = await fetch_installation_token(
                self._client,
                app_id=self._app_id,
                private_key_pem=self._private_key_pem,
                installation_id=installation_id,
            )
            self._tokens[installation_id] = fresh
            return fresh

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
        return (await self.installation_token(installation_id)).token

    async def token_for_repo(self, repo: str) -> str:
        installation_id = await self.installation_id_for(repo)
        return await self.token_for_installation(installation_id)
