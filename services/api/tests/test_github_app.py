"""Unit tests for the GitHub App token-minting module (ADR-0049).

Pure unit tests: an in-memory RSA key signs/verifies the App JWT, and an
``httpx.MockTransport`` stands in for the GitHub API. No network, no PAT.
"""

from __future__ import annotations

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
