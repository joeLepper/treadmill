"""Tests for cc-relay.py — file-drop inter-session relay."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location(
    "cc_relay", Path(__file__).resolve().parents[1] / "cc-relay.py"
)
cc_relay = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules["cc_relay"] = cc_relay
spec.loader.exec_module(cc_relay)  # type: ignore[union-attr]


def test_relay_text_message(tmp_path: Path) -> None:
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", "treadmill-carla", "Hello Carla"]
        cc_relay.main()

    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text() == "Hello Carla"


def test_relay_file_message(tmp_path: Path) -> None:
    msg_file = tmp_path / "handoff.md"
    msg_file.write_text("# Handoff\n\nContext here.")

    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", "treadmill-carla", "--file", str(msg_file)]
        cc_relay.main()

    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "handoff.md:" in content
    assert "Context here." in content


def test_relay_from_prefix(tmp_path: Path) -> None:
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--from", "treadmill-alan",
            "Context",
        ]
        cc_relay.main()

    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    assert files[0].read_text().startswith("[from: treadmill-alan]")


def test_truncation_at_4096(tmp_path: Path) -> None:
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", "treadmill-carla", "x" * 5000]
        cc_relay.main()

    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert len(content) == 4096
    assert content.endswith("[…]")


def test_relay_creates_inbox_dir(tmp_path: Path) -> None:
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", "treadmill-donna", "test"]
        cc_relay.main()

    assert (tmp_path / ".cc-channels" / "treadmill-donna" / "relay").is_dir()


def test_missing_file_arg_exits(tmp_path: Path) -> None:
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", "treadmill-carla", "--file", "/no/such/file.md"]
        with pytest.raises(SystemExit) as exc_info:
            cc_relay.main()
        assert exc_info.value.code != 0
