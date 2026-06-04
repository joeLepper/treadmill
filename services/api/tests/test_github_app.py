"""Unit tests for the GitHub App token-minting module (ADR-0049).

Pure unit tests: an in-memory RSA key signs/verifies the App JWT, and an
``httpx.MockTransport`` stands in for the GitHub API. No network, no PAT.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from treadmill_api.github_app import (
    InstallationToken,
    InstallationTokenCache,
    RedisInstallationTokenCache,
    build_app_jwt,
    fetch_installation_token,
    resolve_installation_id,
)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def private_key_pem(rsa_key: rsa.RSAPrivateKey) -> str:
    return rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


# ── JWT signing ───────────────────────────────────────────────────────────────


def test_build_app_jwt_structure_and_claims(private_key_pem: str) -> None:
    token = build_app_jwt("3785969", private_key_pem, now=1_000_000)
    header_seg, payload_seg, sig_seg = token.split(".")

    header = json.loads(_b64url_decode(header_seg))
    payload = json.loads(_b64url_decode(payload_seg))

    assert header == {"alg": "RS256", "typ": "JWT"}
    assert payload["iss"] == "3785969"
    assert payload["iat"] == 1_000_000 - 60          # backdated
    assert payload["exp"] == 1_000_000 + 540         # 9-minute TTL
    assert payload["exp"] - payload["iat"] <= 600    # within GitHub's 10-min max


def test_build_app_jwt_signature_verifies(
    rsa_key: rsa.RSAPrivateKey, private_key_pem: str,
) -> None:
    token = build_app_jwt("42", private_key_pem, now=1_000_000)
    header_seg, payload_seg, sig_seg = token.split(".")
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    # Does not raise → signature is valid for this key.
    rsa_key.public_key().verify(
        _b64url_decode(sig_seg), signing_input, padding.PKCS1v15(), hashes.SHA256(),
    )


def test_build_app_jwt_tampered_payload_fails_verification(
    rsa_key: rsa.RSAPrivateKey, private_key_pem: str,
) -> None:
    token = build_app_jwt("42", private_key_pem, now=1_000_000)
    header_seg, _payload_seg, sig_seg = token.split(".")
    forged = base64.urlsafe_b64encode(
        json.dumps({"iss": "999"}).encode()
    ).rstrip(b"=").decode()
    signing_input = f"{header_seg}.{forged}".encode("ascii")
    with pytest.raises(Exception):
        rsa_key.public_key().verify(
            _b64url_decode(sig_seg), signing_input, padding.PKCS1v15(), hashes.SHA256(),
        )


# ── token freshness ───────────────────────────────────────────────────────────


def test_installation_token_freshness() -> None:
    now = datetime.now(timezone.utc)
    assert InstallationToken("t", now + timedelta(hours=1)).is_fresh() is True
    assert InstallationToken("t", now + timedelta(seconds=120)).is_fresh() is False
    assert InstallationToken("t", now - timedelta(seconds=1)).is_fresh() is False


# ── HTTP-backed helpers (MockTransport) ───────────────────────────────────────


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_resolve_installation_id(private_key_pem: str) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"id": 4242})

    async with _client(handler) as client:
        inst = await resolve_installation_id(
            client, app_id="3785969", private_key_pem=private_key_pem,
            repo="joeLepper/treadmill",
        )

    assert inst == 4242
    assert seen["path"] == "/repos/joeLepper/treadmill/installation"
    assert seen["auth"].startswith("Bearer ")          # App JWT, not a PAT


@pytest.mark.asyncio
async def test_fetch_installation_token(private_key_pem: str) -> None:
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/app/installations/4242/access_tokens"
        return httpx.Response(201, json={"token": "ghs_abc123", "expires_at": expiry})

    async with _client(handler) as client:
        tok = await fetch_installation_token(
            client, app_id="3785969", private_key_pem=private_key_pem,
            installation_id=4242,
        )

    assert tok.token == "ghs_abc123"
    assert tok.expires_at.tzinfo is not None
    assert tok.is_fresh() is True


@pytest.mark.asyncio
async def test_cache_reuses_token_within_ttl(private_key_pem: str) -> None:
    calls = {"token": 0}
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls["token"] += 1
        return httpx.Response(201, json={"token": f"ghs_{calls['token']}", "expires_at": expiry})

    async with _client(handler) as client:
        cache = InstallationTokenCache(
            client, app_id="3785969", private_key_pem=private_key_pem,
        )
        first = await cache.token_for_installation(4242)
        second = await cache.token_for_installation(4242)

    assert first == second == "ghs_1"
    assert calls["token"] == 1                          # second call served from cache


@pytest.mark.asyncio
async def test_cache_refreshes_when_stale(private_key_pem: str) -> None:
    calls = {"token": 0}
    near = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls["token"] += 1
        # Always returns a token expiring inside the refresh margin → never fresh.
        return httpx.Response(201, json={"token": f"ghs_{calls['token']}", "expires_at": near})

    async with _client(handler) as client:
        cache = InstallationTokenCache(
            client, app_id="3785969", private_key_pem=private_key_pem,
        )
        await cache.token_for_installation(4242)
        await cache.token_for_installation(4242)

    assert calls["token"] == 2                          # stale token re-minted


@pytest.mark.asyncio
async def test_cache_token_for_repo_resolves_then_mints(private_key_pem: str) -> None:
    calls = {"install": 0, "token": 0}
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            calls["install"] += 1
            return httpx.Response(200, json={"id": 77})
        calls["token"] += 1
        assert "/installations/77/" in request.url.path
        return httpx.Response(201, json={"token": "ghs_repo", "expires_at": expiry})

    async with _client(handler) as client:
        cache = InstallationTokenCache(
            client, app_id="3785969", private_key_pem=private_key_pem,
        )
        tok1 = await cache.token_for_repo("joeLepper/treadmill")
        tok2 = await cache.token_for_repo("joeLepper/treadmill")

    assert tok1 == tok2 == "ghs_repo"
    assert calls["install"] == 1                         # repo→installation cached
    assert calls["token"] == 1                           # token cached


# ── transient-failure retry (2026-06-04 intermittent-502 fix) ─────────────────


@pytest.mark.asyncio
async def test_fetch_retries_transient_then_succeeds(
    private_key_pem: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("treadmill_api.github_app._RETRY_BASE_DELAY_SECONDS", 0.0)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502, text="bad gateway")   # transient
        return httpx.Response(201, json={"token": "ghs_ok", "expires_at": expiry})

    async with _client(handler) as client:
        tok = await fetch_installation_token(
            client, app_id="1", private_key_pem=private_key_pem, installation_id=4242,
        )

    assert tok.token == "ghs_ok"
    assert calls["n"] == 3                               # two 502s retried, third won


@pytest.mark.asyncio
async def test_fetch_does_not_retry_non_transient(
    private_key_pem: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("treadmill_api.github_app._RETRY_BASE_DELAY_SECONDS", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_installation_token(
                client, app_id="1", private_key_pem=private_key_pem,
                installation_id=4242,
            )

    assert calls["n"] == 1                               # 404 is not retryable


@pytest.mark.asyncio
async def test_fetch_exhausts_retries_then_raises(
    private_key_pem: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("treadmill_api.github_app._RETRY_BASE_DELAY_SECONDS", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_installation_token(
                client, app_id="1", private_key_pem=private_key_pem,
                installation_id=4242,
            )

    assert calls["n"] == 4                               # _RETRY_MAX_ATTEMPTS


# ── home-installation resolution (repo-less mint) ─────────────────────────────


@pytest.mark.asyncio
async def test_home_installation_id_caches_lowest(private_key_pem: str) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/app/installations"
        return httpx.Response(200, json=[{"id": 22}, {"id": 7}, {"id": 9}])

    async with _client(handler) as client:
        cache = InstallationTokenCache(
            client, app_id="1", private_key_pem=private_key_pem,
        )
        first = await cache.home_installation_id()
        second = await cache.home_installation_id()

    assert first == second == 7                          # lowest id = home
    assert calls["n"] == 1                               # cached after first resolve


@pytest.mark.asyncio
async def test_home_installation_id_none_raises_lookup(private_key_pem: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with _client(handler) as client:
        cache = InstallationTokenCache(
            client, app_id="1", private_key_pem=private_key_pem,
        )
        with pytest.raises(LookupError):
            await cache.home_installation_id()


# ── 403 secondary-rate-limit retry (the gap PR #156 missed) ───────────────────


@pytest.mark.asyncio
async def test_rate_limited_403_is_retried(
    private_key_pem: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("treadmill_api.github_app._RETRY_BASE_DELAY_SECONDS", 0.0)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            # secondary rate limit: 403 with x-ratelimit-remaining: 0
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0"},
                                  text="rate limited")
        return httpx.Response(201, json={"token": "ghs_ok", "expires_at": expiry})

    async with _client(handler) as client:
        tok = await fetch_installation_token(
            client, app_id="1", private_key_pem=private_key_pem, installation_id=1,
        )
    assert tok.token == "ghs_ok"
    assert calls["n"] == 3                               # two 403-rate-limits retried


@pytest.mark.asyncio
async def test_bare_403_not_retried(
    private_key_pem: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("treadmill_api.github_app._RETRY_BASE_DELAY_SECONDS", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")     # genuine denial, no rate hdr

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_installation_token(
                client, app_id="1", private_key_pem=private_key_pem, installation_id=1,
            )
    assert calls["n"] == 1                               # bare 403 is a hard error


# ── Redis-backed cache (survives restarts, shared fleet-wide) ─────────────────


class _FakeRedis:
    """Minimal async Redis stand-in: get/set(nx,ex)/delete. TTL is recorded but
    not expired (tests don't need expiry)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, *, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, *keys: str):
        for k in keys:
            self.store.pop(k, None)
        return 1


def _token_handler(calls: dict, *, hours: int = 1):
    expiry = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            calls["resolve"] = calls.get("resolve", 0) + 1
            return httpx.Response(200, json={"id": 77})
        if request.url.path == "/app/installations":
            calls["list"] = calls.get("list", 0) + 1
            return httpx.Response(200, json=[{"id": 77}])
        calls["mint"] = calls.get("mint", 0) + 1
        return httpx.Response(
            201, json={"token": f"ghs_{calls['mint']}", "expires_at": expiry},
        )

    return handler


@pytest.mark.asyncio
async def test_redis_cache_mints_once_then_serves_from_redis(
    private_key_pem: str,
) -> None:
    calls: dict = {}
    redis = _FakeRedis()
    async with _client(_token_handler(calls)) as client:
        cache = RedisInstallationTokenCache(
            client, redis, app_id="1", private_key_pem=private_key_pem,
        )
        first = await cache.installation_token(77)
        second = await cache.installation_token(77)

    assert first.token == second.token == "ghs_1"
    assert calls["mint"] == 1                             # second served from redis
    assert "github:install-token:77" in redis.store      # token persisted


@pytest.mark.asyncio
async def test_redis_cache_survives_a_fresh_process(private_key_pem: str) -> None:
    # Two distinct cache instances sharing one Redis = the survives-restart /
    # shared-fleet property: the second never mints.
    calls: dict = {}
    redis = _FakeRedis()
    async with _client(_token_handler(calls)) as client:
        cache_a = RedisInstallationTokenCache(
            client, redis, app_id="1", private_key_pem=private_key_pem,
        )
        await cache_a.installation_token(77)
        cache_b = RedisInstallationTokenCache(
            client, redis, app_id="1", private_key_pem=private_key_pem,
        )
        tok = await cache_b.installation_token(77)

    assert tok.token == "ghs_1"
    assert calls["mint"] == 1                             # shared token, no re-mint


@pytest.mark.asyncio
async def test_redis_cache_single_flight_under_concurrency(
    private_key_pem: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "treadmill_api.github_app._REDIS_SINGLEFLIGHT_POLL_SECONDS", 0.01,
    )
    calls: dict = {}
    redis = _FakeRedis()
    async with _client(_token_handler(calls)) as client:
        cache = RedisInstallationTokenCache(
            client, redis, app_id="1", private_key_pem=private_key_pem,
        )
        results = await asyncio.gather(
            *[cache.installation_token(77) for _ in range(8)]
        )

    assert {r.token for r in results} == {"ghs_1"}        # all got the one token
    assert calls["mint"] == 1                             # single-flight: one mint


@pytest.mark.asyncio
async def test_redis_cache_repo_and_home_ids_cached(private_key_pem: str) -> None:
    calls: dict = {}
    redis = _FakeRedis()
    async with _client(_token_handler(calls)) as client:
        cache = RedisInstallationTokenCache(
            client, redis, app_id="1", private_key_pem=private_key_pem,
        )
        assert await cache.installation_id_for("o/n") == 77
        assert await cache.installation_id_for("o/n") == 77      # 2nd from redis
        assert await cache.home_installation_id() == 77
        assert await cache.home_installation_id() == 77          # 2nd from redis

    assert calls["resolve"] == 1
    assert calls["list"] == 1


@pytest.mark.asyncio
async def test_redis_cache_degrades_on_redis_failure(private_key_pem: str) -> None:
    # A Redis that raises on every op must NOT break minting (degrade, not 500).
    class _BrokenRedis:
        async def get(self, *a, **k):
            raise RuntimeError("redis down")

        async def set(self, *a, **k):
            raise RuntimeError("redis down")

        async def delete(self, *a, **k):
            raise RuntimeError("redis down")

    calls: dict = {}
    async with _client(_token_handler(calls)) as client:
        cache = RedisInstallationTokenCache(
            client, _BrokenRedis(), app_id="1", private_key_pem=private_key_pem,
        )
        tok = await cache.installation_token(77)

    assert tok.token == "ghs_1"                           # minted despite redis down
    assert calls["mint"] == 1
