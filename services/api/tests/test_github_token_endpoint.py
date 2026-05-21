"""Unit tests for the installation-token minting endpoint (ADR-0049 phase 5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api import github_app
from treadmill_api.github_app import InstallationToken
from treadmill_api.routers.github import router


def _client(settings: SimpleNamespace) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings
    return TestClient(app)


def _configured() -> SimpleNamespace:
    return SimpleNamespace(github_app_id="3785969", github_app_private_key="-PEM-")


@pytest.fixture
def _token(monkeypatch):
    tok = InstallationToken("ghs_minted", datetime.now(timezone.utc) + timedelta(hours=1))
    monkeypatch.setattr(github_app, "fetch_installation_token", AsyncMock(return_value=tok))
    return tok


def test_with_repo_resolves_and_mints(monkeypatch, _token) -> None:
    monkeypatch.setattr(github_app, "resolve_installation_id", AsyncMock(return_value=4242))
    resp = _client(_configured()).post("/api/v1/github/installation-token", json={"repo": "o/n"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == "ghs_minted"
    assert body["installation_id"] == 4242
    assert body["repo"] == "o/n"


def test_no_repo_uses_sole_installation(monkeypatch, _token) -> None:
    monkeypatch.setattr(github_app, "list_installation_ids", AsyncMock(return_value=[77]))
    resp = _client(_configured()).post("/api/v1/github/installation-token", json={})
    assert resp.status_code == 200
    assert resp.json()["installation_id"] == 77


def test_no_repo_multiple_installations_uses_home(monkeypatch, _token) -> None:
    # No repo + several installations → default to the home (lowest-id)
    # installation rather than 400, so the worker's startup mint succeeds on a
    # multi-installation deployment (HOTFIX 2026-05-21).
    monkeypatch.setattr(github_app, "list_installation_ids", AsyncMock(return_value=[22, 7, 9]))
    resp = _client(_configured()).post("/api/v1/github/installation-token", json={})
    assert resp.status_code == 200
    assert resp.json()["installation_id"] == 7


def test_no_repo_no_installations_is_503(monkeypatch, _token) -> None:
    monkeypatch.setattr(github_app, "list_installation_ids", AsyncMock(return_value=[]))
    resp = _client(_configured()).post("/api/v1/github/installation-token", json={})
    assert resp.status_code == 503


def test_app_not_configured_is_503() -> None:
    settings = SimpleNamespace(github_app_id=None, github_app_private_key=None)
    resp = _client(settings).post("/api/v1/github/installation-token", json={})
    assert resp.status_code == 503


def test_github_error_is_502(monkeypatch) -> None:
    req = httpx.Request("GET", "https://api.github.com/app/installations")
    err = httpx.HTTPStatusError("nope", request=req, response=httpx.Response(404, request=req))
    monkeypatch.setattr(github_app, "list_installation_ids", AsyncMock(side_effect=err))
    resp = _client(_configured()).post("/api/v1/github/installation-token", json={})
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_list_installation_ids_helper() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json=[{"id": 11}, {"id": 22}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ids = await github_app.list_installation_ids(
            client, app_id="3785969",
            private_key_pem=_RSA_PEM,
        )
    assert ids == [11, 22]


# A throwaway RSA key for the JWT signing inside list_installation_ids.
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

_RSA_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    .decode()
)
