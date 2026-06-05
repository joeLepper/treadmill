"""Tests for cc-relay.py (ADR-0067/0068).

Cross-session Telegram relay: send a message or file to another labeled
session's Telegram channel.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import cc-relay using importlib due to hyphenated filename
spec = importlib.util.spec_from_file_location(
    "cc_relay", Path(__file__).resolve().parents[1] / "cc-relay.py"
)
cc_relay = importlib.util.module_from_spec(spec)
sys.modules["cc_relay"] = cc_relay
spec.loader.exec_module(cc_relay)


@patch("cc_relay.urllib.request.urlopen")
def test_send_text_message(mock_urlopen, tmp_path):
    """Happy path: send a plain text message."""
    # Mock successful response
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = json.dumps({
        "result": {"message_id": 42}
    }).encode()
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None
    mock_urlopen.return_value = mock_response

    message_id = cc_relay.send_message("98765", "Hello world", "test_token_123")

    assert message_id == "42"
    mock_urlopen.assert_called_once()
    call_args = mock_urlopen.call_args
    assert call_args[0][0].full_url == "https://api.telegram.org/bottest_token_123/sendMessage"
    payload = json.loads(call_args[0][0].data.decode())
    assert payload["chat_id"] == "98765"
    assert payload["text"] == "Hello world"
    assert payload["parse_mode"] == "Markdown"


@patch("cc_relay.urllib.request.urlopen")
def test_send_file_message(mock_urlopen, tmp_path):
    """Send a file's contents with filename prefix."""
    label = "test-session"
    state_dir = tmp_path / ".cc-channels" / label
    state_dir.mkdir(parents=True)
    env_file = state_dir / "telegram.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=token\nTELEGRAM_CHAT_ID=12345\n")

    message_file = tmp_path / "test_message.txt"
    message_file.write_text("This is the file content")

    # Mock response
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = json.dumps({
        "result": {"message_id": 99}
    }).encode()
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None
    mock_urlopen.return_value = mock_response

    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", label, "--file", str(message_file)]
        cc_relay.main()

    mock_urlopen.assert_called_once()
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode())
    assert "📄 test_message.txt:" in payload["text"]
    assert "This is the file content" in payload["text"]


@patch("cc_relay.urllib.request.urlopen")
def test_truncation_at_4096(mock_urlopen, tmp_path):
    """Messages longer than 4096 chars are truncated."""
    label = "test-session"
    state_dir = tmp_path / ".cc-channels" / label
    state_dir.mkdir(parents=True)
    env_file = state_dir / "telegram.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=token\nTELEGRAM_CHAT_ID=12345\n")

    # Create a message longer than 4096 chars
    long_message = "x" * 5000

    # Mock response
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = json.dumps({
        "result": {"message_id": 100}
    }).encode()
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None
    mock_urlopen.return_value = mock_response

    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", label, long_message]
        cc_relay.main()

    payload = json.loads(mock_urlopen.call_args[0][0].data.decode())
    text = payload["text"]
    assert len(text) == 4096
    assert text.endswith("[…]")


@patch("cc_relay.urllib.request.urlopen")
def test_from_prefix(mock_urlopen, tmp_path):
    """When --from is given, message is prefixed with [from: <label>]."""
    label = "target-session"
    state_dir = tmp_path / ".cc-channels" / label
    state_dir.mkdir(parents=True)
    env_file = state_dir / "telegram.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=token\nTELEGRAM_CHAT_ID=999\n")

    # Mock response
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = json.dumps({
        "result": {"message_id": 200}
    }).encode()
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None
    mock_urlopen.return_value = mock_response

    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", label, "--from", "source-session", "Hello"]
        cc_relay.main()

    payload = json.loads(mock_urlopen.call_args[0][0].data.decode())
    assert "[from: source-session]" in payload["text"]


def test_missing_chat_id_exits(tmp_path):
    """Exit with error if TELEGRAM_CHAT_ID is missing."""
    label = "test-session"
    state_dir = tmp_path / ".cc-channels" / label
    state_dir.mkdir(parents=True)
    env_file = state_dir / "telegram.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=token\n")  # No CHAT_ID

    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", label, "test message"]
        with pytest.raises(SystemExit) as exc_info:
            cc_relay.main()
        assert exc_info.value.code != 0


def test_missing_env_file_exits(tmp_path):
    """Exit with error if env file does not exist."""
    label = "nonexistent-session"

    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", label, "test message"]
        with pytest.raises(SystemExit) as exc_info:
            cc_relay.main()
        assert exc_info.value.code != 0
