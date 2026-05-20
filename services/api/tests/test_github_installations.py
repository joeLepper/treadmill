"""Unit tests for the onboarded-repo installation registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from treadmill_api.github_installations import InstallationRegistry


def test_record_and_is_onboarded_and_known() -> None:
    registry = InstallationRegistry()

    assert registry.is_onboarded("joeLepper/treadmill") is False
    assert registry.known() == {}

    registry.record("joeLepper/treadmill", 4242)
    registry.record("anthropics/claude-code", 7777)

    assert registry.is_onboarded("joeLepper/treadmill") is True
    assert registry.is_onboarded("anthropics/claude-code") is True
    assert registry.is_onboarded("unknown/repo") is False

    snapshot = registry.known()
    assert snapshot == {
        "joeLepper/treadmill": 4242,
        "anthropics/claude-code": 7777,
    }
    # ``known()`` returns a copy — mutating it must not affect the registry.
    snapshot["mutated/repo"] = 1
    assert registry.known() == {
        "joeLepper/treadmill": 4242,
        "anthropics/claude-code": 7777,
    }


@pytest.mark.asyncio
async def test_resolve_caches_after_first_call() -> None:
    registry = InstallationRegistry()

    with patch(
        "treadmill_api.github_installations.resolve_installation_id",
        new=AsyncMock(return_value=4242),
    ) as mock_resolve:
        first = await registry.resolve(
            client=None,
            app_id="3785969",
            private_key_pem="pem",
            repo="joeLepper/treadmill",
        )
        second = await registry.resolve(
            client=None,
            app_id="3785969",
            private_key_pem="pem",
            repo="joeLepper/treadmill",
        )

    assert first == 4242
    assert second == 4242
    assert mock_resolve.call_count == 1
    assert registry.is_onboarded("joeLepper/treadmill") is True
    assert registry.known() == {"joeLepper/treadmill": 4242}


@pytest.mark.asyncio
async def test_resolve_uses_recorded_id_without_calling_upstream() -> None:
    registry = InstallationRegistry()
    registry.record("joeLepper/treadmill", 9999)

    with patch(
        "treadmill_api.github_installations.resolve_installation_id",
        new=AsyncMock(return_value=4242),
    ) as mock_resolve:
        installation_id = await registry.resolve(
            client=None,
            app_id="3785969",
            private_key_pem="pem",
            repo="joeLepper/treadmill",
        )

    assert installation_id == 9999
    assert mock_resolve.call_count == 0
