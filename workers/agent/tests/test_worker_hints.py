"""Tests for the worker hints tool (ADR-0081 §2)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest

from treadmill_agent.worker_hints import RequestHintTool, RequestHintToolResult


def test_request_hint_tool_writes_relay_file(tmp_path: Path) -> None:
    """Tool writes relay file with correct path and format."""
    # Mock home directory
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    with patch("pathlib.Path.home", return_value=home_dir):
        tool = RequestHintTool(
            api_base_url="http://api:5000",
            task_id="task-123",
            worker_step_id="step-456",
            created_by="operator-carla",
        )

        # Mock the POST call to avoid network
        with patch.object(httpx.Client, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            result = tool.request_hint(
                reason="tests_need_scope",
                context="The test count needs bumping.",
            )

            # Verify result
            assert result.acknowledged is True

        # Verify relay file was written
        relay_dir = home_dir / ".cc-channels" / "operator-carla" / "relay"
        assert relay_dir.exists()

        # Find the file (name is timestamp-based)
        relay_files = list(relay_dir.glob("*.md"))
        assert len(relay_files) == 1

        relay_file = relay_files[0]
        content = relay_file.read_text()

        # Verify content format
        assert "# Worker hint request" in content
        assert "## Reason" in content
        assert "tests_need_scope" in content
        assert "## Context" in content
        assert "The test count needs bumping." in content
        assert "[from: worker-task-123]" in content
        assert "step-456" in content


def test_request_hint_tool_posts_event() -> None:
    """Tool POSTs event to API endpoint."""
    tool = RequestHintTool(
        api_base_url="http://api:5000",
        task_id="task-123",
        worker_step_id="step-456",
        created_by="operator-carla",
    )

    with patch.object(httpx.Client, "post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool.request_hint(
            reason="tests_need_scope",
            context="The test count needs bumping.",
        )

        # Verify POST was called
        mock_post.assert_called_once()
        call_args = mock_post.call_args

        # Check URL
        url = call_args[0][0]
        assert url == "http://api:5000/api/v1/tasks/task-123/worker_hint_request"

        # Check payload
        json_payload = call_args[1]["json"]
        assert json_payload["reason"] == "tests_need_scope"
        assert json_payload["context_excerpt"] == "The test count needs bumping."
        assert json_payload["worker_step_id"] == "step-456"


def test_request_hint_tool_returns_nonblocking(tmp_path: Path) -> None:
    """Tool returns immediately without waiting for operator."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    with patch("pathlib.Path.home", return_value=home_dir):
        tool = RequestHintTool(
            api_base_url="http://api:5000",
            task_id="task-123",
            worker_step_id="step-456",
            created_by="operator-carla",
        )

        with patch.object(httpx.Client, "post") as mock_post:
            mock_post.side_effect = Exception("Network error")

            # Should still return success despite POST failure
            result = tool.request_hint(
                reason="stuck",
                context="Can't proceed",
            )

            assert result.acknowledged is True

        # But the relay file should still be written
        relay_dir = home_dir / ".cc-channels" / "operator-carla" / "relay"
        assert len(list(relay_dir.glob("*.md"))) == 1


def test_request_hint_tool_truncates_reason() -> None:
    """Tool truncates reason to 100 chars."""
    tool = RequestHintTool(
        api_base_url="http://api:5000",
        task_id="task-123",
        worker_step_id="step-456",
        created_by="operator-carla",
    )

    long_reason = "x" * 200

    with patch.object(httpx.Client, "post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool.request_hint(reason=long_reason, context="context")

        # Verify the reason was truncated in the POST
        call_args = mock_post.call_args
        json_payload = call_args[1]["json"]
        assert len(json_payload["reason"]) == 100


def test_request_hint_tool_truncates_context() -> None:
    """Tool truncates context to 2000 chars."""
    tool = RequestHintTool(
        api_base_url="http://api:5000",
        task_id="task-123",
        worker_step_id="step-456",
        created_by="operator-carla",
    )

    long_context = "x" * 3000

    with patch.object(httpx.Client, "post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool.request_hint(reason="stuck", context=long_context)

        # Verify the context was truncated
        call_args = mock_post.call_args
        json_payload = call_args[1]["json"]
        assert len(json_payload["context_excerpt"]) == 500  # Excerpt is max 500 chars


def test_request_hint_tool_relay_file_format(tmp_path: Path) -> None:
    """Relay file has correct header and footer format."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    with patch("pathlib.Path.home", return_value=home_dir):
        tool = RequestHintTool(
            api_base_url="http://api:5000",
            task_id="task-abcd1234",
            worker_step_id="step-5678",
            created_by="operator-bob",
        )

        with patch.object(httpx.Client, "post"):
            tool.request_hint(
                reason="alembic_heads_unclear",
                context="Two alembic heads found. Can't determine which to use.",
            )

        relay_dir = home_dir / ".cc-channels" / "operator-bob" / "relay"
        relay_file = list(relay_dir.glob("*.md"))[0]
        content = relay_file.read_text()

        # Verify header
        assert content.startswith("# Worker hint request\n")

        # Verify reason section
        assert "\n## Reason\n\nalembic_heads_unclear\n" in content

        # Verify context section
        assert "\n## Context\n\nTwo alembic heads found" in content

        # Verify footer metadata
        assert "[from: worker-task-abcd1234]" in content
        assert "Worker step: step-5678" in content
