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


def _is_rate_limited(response: httpx.Response) -> bool:
    """True when a response is a GitHub rate limit we should back off + retry.

    GitHub signals its **secondary** rate limit as a ``403`` (not ``429``) with
    a ``Retry-After`` header or ``x-ratelimit-remaining: 0`` — the case PR #156
    missed (its retry set only covered 429/5xx), which is why token-mint 502s
    persisted with zero retry warnings. A bare ``403`` (genuine permission
    denial) has neither header and is NOT retried.
    """
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("Retry-After"):
        return True
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "secondary rate limit" in (response.text or "").lower()


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
            retryable = (
                status_code in _RETRYABLE_STATUSES
                or _is_rate_limited(exc.response)
            )
            if not retryable or attempt == _RETRY_MAX_ATTEMPTS - 1:
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


# ── Redis-backed cache (ADR-0049; 2026-06-04 durable fix) ─────────────────────
#
# An installation token is a ~1h reusable bearer token; GitHub's own docs say to
# cache + reuse rather than re-mint. The in-process cache above dies on every API
# recreate (the deploy-watcher recreates ``treadmill-api`` on every services/api
# merge), so the fleet cold-starts and re-mints en masse → GitHub secondary rate
# limit (403) → 502s. Backing the cache with Redis makes one token per
# installation **survive restarts and be shared fleet-wide**, collapsing GitHub
# mint volume to ~1/installation/hour. Redis is an ENHANCEMENT, never a hard
# dependency: any Redis error degrades to a direct mint (never a 500). Token
# values live only in Redis (internal-only) and are never logged.

_REDIS_TOKEN_KEY = "github:install-token:{installation_id}"
_REDIS_REPO_ID_KEY = "github:install-id:{repo}"
_REDIS_HOME_ID_KEY = "github:install-id:__home__"
_REDIS_MINT_LOCK_KEY = "github:mint-lock:{installation_id}"
_REDIS_INSTALL_MAP_TTL_SECONDS = 6 * 3600  # repo→installation rarely changes
_REDIS_MINT_LOCK_TTL_SECONDS = 10
_REDIS_SINGLEFLIGHT_POLL_SECONDS = 0.25
_REDIS_SINGLEFLIGHT_BUDGET_SECONDS = 8.0


def _as_str(raw: Any) -> str | None:
    """Normalize a Redis value (bytes or str, depending on decode_responses)."""
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


class RedisInstallationTokenCache:
    """Installation-token cache backed by Redis (same async surface as
    :class:`InstallationTokenCache`) so tokens survive API restarts and are
    shared across the whole fleet. On any Redis failure it falls back to a
    direct mint — Redis is an optimization, not a dependency."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        redis: Any,
        *,
        app_id: str,
        private_key_pem: str,
    ) -> None:
        self._client = client
        self._redis = redis
        self._app_id = app_id
        self._private_key_pem = private_key_pem

    async def _redis_get(self, key: str) -> str | None:
        try:
            return _as_str(await self._redis.get(key))
        except Exception:
            logger.warning("github_app: redis GET %s failed; resolving fresh",
                           key, exc_info=True)
            return None

    async def _redis_set(self, key: str, value: str, *, ex: int) -> None:
        if ex <= 0:
            return
        try:
            await self._redis.set(key, value, ex=ex)
        except Exception:
            logger.warning("github_app: redis SET %s failed (continuing)",
                           key, exc_info=True)

    async def installation_id_for(self, repo: str) -> int:
        key = _REDIS_REPO_ID_KEY.format(repo=repo)
        cached = await self._redis_get(key)
        if cached is not None:
            return int(cached)
        installation_id = await resolve_installation_id(
            self._client, app_id=self._app_id,
            private_key_pem=self._private_key_pem, repo=repo,
        )
        await self._redis_set(key, str(installation_id),
                              ex=_REDIS_INSTALL_MAP_TTL_SECONDS)
        return installation_id

    async def home_installation_id(self) -> int:
        cached = await self._redis_get(_REDIS_HOME_ID_KEY)
        if cached is not None:
            return int(cached)
        ids = await list_installation_ids(
            self._client, app_id=self._app_id,
            private_key_pem=self._private_key_pem,
        )
        if not ids:
            raise LookupError("GitHub App has no installations")
        home = min(ids)
        await self._redis_set(_REDIS_HOME_ID_KEY, str(home),
                              ex=_REDIS_INSTALL_MAP_TTL_SECONDS)
        return home

    async def _read_token(self, key: str) -> InstallationToken | None:
        raw = await self._redis_get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            tok = InstallationToken(
                token=data["token"],
                expires_at=datetime.fromisoformat(data["expires_at"]),
            )
        except (ValueError, KeyError, TypeError):
            return None  # corrupt entry — treat as a miss
        return tok if tok.is_fresh() else None

    async def _store_token(self, key: str, tok: InstallationToken) -> None:
        ttl = int(
            (tok.expires_at - datetime.now(timezone.utc)).total_seconds()
            - _TOKEN_REFRESH_MARGIN_SECONDS
        )
        await self._redis_set(
            key,
            json.dumps({"token": tok.token,
                        "expires_at": tok.expires_at.isoformat()}),
            ex=ttl,
        )

    async def installation_token(self, installation_id: int) -> InstallationToken:
        key = _REDIS_TOKEN_KEY.format(installation_id=installation_id)
        tok = await self._read_token(key)
        if tok is not None:
            return tok
        return await self._mint_single_flight(key, installation_id)

    async def _mint_single_flight(
        self, key: str, installation_id: int,
    ) -> InstallationToken:
        """Mint with a fleet-wide single-flight lock so a cold-cache burst
        produces ONE GitHub mint, not N. Lock losers poll the token key (the
        winner's write lands); if the budget elapses they mint anyway — a rare
        double-mint is harmless (tokens are independently valid), a stall is not.
        """
        lock_key = _REDIS_MINT_LOCK_KEY.format(installation_id=installation_id)
        got_lock = True
        try:
            got_lock = bool(
                await self._redis.set(
                    lock_key, "1", nx=True, ex=_REDIS_MINT_LOCK_TTL_SECONDS,
                )
            )
        except Exception:
            logger.warning("github_app: redis mint-lock failed; minting directly",
                           exc_info=True)
            got_lock = True

        if not got_lock:
            waited = 0.0
            while waited < _REDIS_SINGLEFLIGHT_BUDGET_SECONDS:
                await asyncio.sleep(_REDIS_SINGLEFLIGHT_POLL_SECONDS)
                waited += _REDIS_SINGLEFLIGHT_POLL_SECONDS
                tok = await self._read_token(key)
                if tok is not None:
                    return tok
            # budget exhausted — fall through and mint anyway

        fresh = await fetch_installation_token(
            self._client, app_id=self._app_id,
            private_key_pem=self._private_key_pem,
            installation_id=installation_id,
        )
        await self._store_token(key, fresh)
        if got_lock:
            try:
                await self._redis.delete(lock_key)
            except Exception:
                pass  # lock self-expires via EX
        return fresh

    async def token_for_installation(self, installation_id: int) -> str:
        return (await self.installation_token(installation_id)).token

    async def token_for_repo(self, repo: str) -> str:
        installation_id = await self.installation_id_for(repo)
        return await self.token_for_installation(installation_id)
