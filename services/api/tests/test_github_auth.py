"""Unit tests for the GitHub auth provider selector (ADR-0049).

The factory picks the App path when both ``github_app_id`` and
``github_app_private_key`` are set, and the legacy PAT path otherwise. We
patch ``InstallationTokenCache`` for the App case so no network is touched
and no real RSA key is needed. A ``SimpleNamespace`` stands in for
``Settings`` to keep these tests independent of ambient env vars.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from treadmill_api.github_auth import build_github_auth_provider


def _settings(
    *,
    github_token: str | None = None,
    github_app_id: str | None = None,
    github_app_private_key: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        github_token=github_token,
        github_app_id=github_app_id,
        github_app_private_key=github_app_private_key,
    )


@pytest.mark.asyncio
async def test_pat_path_returns_configured_token() -> None:
    settings = _settings(github_token="ghp_legacy_pat")
    async with httpx.AsyncClient() as client:
        provider = build_github_auth_provider(settings, client)
        token = await provider.token_for_repo("a/b")

    assert token == "ghp_legacy_pat"


@pytest.mark.asyncio
async def test_pat_path_when_only_app_id_set() -> None:
    # Half-configured App settings must NOT activate the App path — both
    # id and private key are required.
    settings = _settings(github_token="ghp_legacy_pat", github_app_id="3785969")
    async with httpx.AsyncClient() as client:
        provider = build_github_auth_provider(settings, client)
        token = await provider.token_for_repo("a/b")

    assert token == "ghp_legacy_pat"


@pytest.mark.asyncio
async def test_app_path_returns_installation_token_not_pat() -> None:
    dummy_key = "-----BEGIN DUMMY KEY-----\n-----END DUMMY KEY-----\n"
    settings = _settings(
        github_token="ghp_legacy_pat",
        github_app_id="3785969",
        github_app_private_key=dummy_key,
    )

    fake_cache = AsyncMock()
    fake_cache.token_for_repo.return_value = "ghs_installation_xyz"

    with patch(
        "treadmill_api.github_auth.InstallationTokenCache",
        return_value=fake_cache,
    ) as cache_cls:
        async with httpx.AsyncClient() as client:
            provider = build_github_auth_provider(settings, client)
            token = await provider.token_for_repo("a/b")

    assert token == "ghs_installation_xyz"
    assert token != "ghp_legacy_pat"
    cache_cls.assert_called_once_with(
        client, app_id="3785969", private_key_pem=dummy_key,
    )
    fake_cache.token_for_repo.assert_awaited_once_with("a/b")
