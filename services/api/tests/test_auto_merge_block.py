"""Unit tests for ``_repo_auto_merge_blocked`` (ADR-0050 d.5).

Covers the repo-level auto-merge gate that the live trigger consults in
addition to the plan-level flag. The helper must fail OPEN — any error
or missing config should fall through to the pre-ADR-0050 behavior
(allow auto-merge).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from treadmill_api.coordination.triggers import _repo_auto_merge_blocked
from treadmill_api.repo_config import RepoConfig


@pytest.mark.asyncio
async def test_returns_true_when_repo_config_blocks() -> None:
    config = RepoConfig(repo="acme/repo", auto_merge_blocked=True)
    with patch(
        "treadmill_api.coordination.triggers.OnboardingStore"
    ) as mock_store_cls:
        mock_store_cls.return_value.get_repo_config = AsyncMock(
            return_value=config,
        )
        assert await _repo_auto_merge_blocked(object(), "acme/repo") is True


@pytest.mark.asyncio
async def test_returns_false_when_repo_config_allows() -> None:
    config = RepoConfig(repo="acme/repo", auto_merge_blocked=False)
    with patch(
        "treadmill_api.coordination.triggers.OnboardingStore"
    ) as mock_store_cls:
        mock_store_cls.return_value.get_repo_config = AsyncMock(
            return_value=config,
        )
        assert await _repo_auto_merge_blocked(object(), "acme/repo") is False


@pytest.mark.asyncio
async def test_returns_false_when_no_repo_config() -> None:
    with patch(
        "treadmill_api.coordination.triggers.OnboardingStore"
    ) as mock_store_cls:
        mock_store_cls.return_value.get_repo_config = AsyncMock(
            return_value=None,
        )
        assert await _repo_auto_merge_blocked(object(), "acme/repo") is False


@pytest.mark.asyncio
async def test_fails_open_when_store_raises() -> None:
    with patch(
        "treadmill_api.coordination.triggers.OnboardingStore"
    ) as mock_store_cls:
        mock_store_cls.return_value.get_repo_config = AsyncMock(
            side_effect=RuntimeError("db is down"),
        )
        assert await _repo_auto_merge_blocked(object(), "acme/repo") is False


@pytest.mark.asyncio
async def test_empty_repo_short_circuits_without_store_call() -> None:
    with patch(
        "treadmill_api.coordination.triggers.OnboardingStore"
    ) as mock_store_cls:
        mock_store_cls.return_value.get_repo_config = AsyncMock()
        assert await _repo_auto_merge_blocked(object(), "") is False
        mock_store_cls.return_value.get_repo_config.assert_not_called()
