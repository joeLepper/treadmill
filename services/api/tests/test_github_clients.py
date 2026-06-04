"""Unit tests for the ADR-0049 cutover client builder + auth hook.

Covers ``build_github_clients`` (App vs PAT vs none), the per-repo
``_InstallationAuthHook``, and ``extract_repo_from_github_path``. No network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from treadmill_api.github_auth import (
    GitHubClients,
    _InstallationAuthHook,
    build_github_clients,
    extract_repo_from_github_path,
)


def _settings(*, token=None, app_id=None, private_key=None) -> SimpleNamespace:
    return SimpleNamespace(
        github_token=token,
        github_app_id=app_id,
        github_app_private_key=private_key,
    )


# ── path extraction ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/repos/joeLepper/treadmill/pulls/210/merge", "joeLepper/treadmill"),
        ("/repos/o/n", "o/n"),
        ("/repos/o/n/issues/1/comments", "o/n"),
        ("/app/installations/5/access_tokens", None),
        ("/repos/onlyone", None),
        ("", None),
    ],
)
def test_extract_repo_from_github_path(path: str, expected: str | None) -> None:
    assert extract_repo_from_github_path(path) == expected


# ── client builder ────────────────────────────────────────────────────────────


def test_build_pat_path_static_header_no_hook() -> None:
    clients = build_github_clients(_settings(token="ghp_pat"))
    assert clients.client is not None
    assert clients.client.headers["authorization"] == "Bearer ghp_pat"
    assert clients.client.event_hooks.get("request", []) == []  # no per-repo hook
    assert clients._aux == []


def test_build_app_path_hook_no_static_auth() -> None:
    clients = build_github_clients(
        _settings(app_id="3785969", private_key="-----PEM-----")
    )
    assert clients.client is not None
    # No static Authorization header — the hook stamps it per request.
    assert "authorization" not in clients.client.headers
    hooks = clients.client.event_hooks.get("request", [])
    assert len(hooks) == 1 and isinstance(hooks[0], _InstallationAuthHook)
    # A dedicated minting client exists and is tracked for close.
    assert len(clients._aux) == 1


def test_build_app_path_takes_precedence_over_pat() -> None:
    clients = build_github_clients(
        _settings(token="ghp_pat", app_id="42", private_key="-----PEM-----")
    )
    assert "authorization" not in clients.client.headers  # App path, not PAT
    assert len(clients.client.event_hooks["request"]) == 1


def test_build_none_when_unconfigured() -> None:
    assert build_github_clients(_settings()).client is None


def test_app_path_uses_in_process_cache_without_redis() -> None:
    from treadmill_api.github_app import InstallationTokenCache

    clients = build_github_clients(
        _settings(app_id="42", private_key="-----PEM-----")
    )
    assert isinstance(clients.installation_cache, InstallationTokenCache)


def test_app_path_uses_redis_cache_when_redis_supplied() -> None:
    from treadmill_api.github_app import RedisInstallationTokenCache

    clients = build_github_clients(
        _settings(app_id="42", private_key="-----PEM-----"),
        redis_client=object(),     # any non-None redis selects the Redis backend
    )
    assert isinstance(clients.installation_cache, RedisInstallationTokenCache)


# ── auth hook ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hook_stamps_per_repo_token() -> None:
    provider = SimpleNamespace(token_for_repo=AsyncMock(return_value="ghs_inst"))
    hook = _InstallationAuthHook(provider)
    req = httpx.Request("PUT", "https://api.github.com/repos/o/n/pulls/1/merge")
    await hook(req)
    assert req.headers["authorization"] == "Bearer ghs_inst"
    provider.token_for_repo.assert_awaited_once_with("o/n")


@pytest.mark.asyncio
async def test_hook_skips_non_repo_paths() -> None:
    provider = SimpleNamespace(token_for_repo=AsyncMock(return_value="ghs_inst"))
    hook = _InstallationAuthHook(provider)
    req = httpx.Request("POST", "https://api.github.com/app/installations/5/access_tokens")
    await hook(req)
    assert "authorization" not in req.headers
    provider.token_for_repo.assert_not_awaited()


# ── close ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_closes_client_and_aux() -> None:
    main = AsyncMock()
    aux = AsyncMock()
    await GitHubClients(client=main, _aux=[aux]).aclose()
    main.aclose.assert_awaited_once()
    aux.aclose.assert_awaited_once()
