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


# ── Trust gates (docs/plans/2026-06-05-cc-relay-trust-gates.md) ───────────────


def test_default_type_is_context_no_header(tmp_path: Path) -> None:
    """Absent --type, the message ships without the [ACTION REQUEST] header
    so existing context-delivery callers don't suddenly look like action
    requests."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", "treadmill-carla", "context body"]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert content == "context body"
    assert cc_relay.ACTION_HEADER not in content


def test_explicit_context_type_no_header(tmp_path: Path) -> None:
    """`--type context` is the documented spelling for the default; it must
    behave identically to omitting the flag."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--type", "context",
            "context body",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    assert files[0].read_text() == "context body"


def test_action_type_prepends_header(tmp_path: Path) -> None:
    """`--type action` prepends the literal `[ACTION REQUEST]` header on its
    own line, followed by a blank line, before the message body. The
    receiving session pattern-matches on this header to gate execution."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--type", "action",
            "restart your unit",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert content == f"{cc_relay.ACTION_HEADER}\n\nrestart your unit"
    assert content.startswith(cc_relay.ACTION_HEADER)


def test_action_header_before_from_prefix(tmp_path: Path) -> None:
    """When --type action and --from are both set, the action header must
    land on line 1 — BEFORE the [from:] prefix — so a receiver's pattern-
    match for the action signal is positional and source-label-agnostic."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--from", "treadmill-alan",
            "--type", "action",
            "restart your unit",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    first_line = content.split("\n", 1)[0]
    assert first_line == cc_relay.ACTION_HEADER
    assert "[from: treadmill-alan]" in content
    # Header strictly precedes the from-prefix in the body.
    assert content.index(cc_relay.ACTION_HEADER) < content.index("[from:")


def test_invalid_type_rejected(tmp_path: Path) -> None:
    """argparse choices= rejects anything outside the closed enum so a
    typo doesn't silently fall back to context."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--type", "command",  # not in ALLOWED_TYPES
            "ignored",
        ]
        with pytest.raises(SystemExit):
            cc_relay.main()


def test_action_header_persists_through_truncation(tmp_path: Path) -> None:
    """When --type action and the body is long enough to truncate, the
    header on line 1 must be preserved — receivers depend on it. The
    truncation eats body tail, not the header."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--type", "action",
            "x" * 5000,
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert len(content) == 4096
    assert content.startswith(cc_relay.ACTION_HEADER)
    assert content.endswith("[…]")


def test_action_type_filename_contains_action(tmp_path: Path) -> None:
    """When --type action, the written filename includes '-action' so
    recipients can filter by filename without reading content."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--type", "action",
            "test message",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    assert len(files) == 1
    assert "-action" in files[0].name


def test_context_type_filename_no_action(tmp_path: Path) -> None:
    """When --type context (explicit), the filename does not include
    '-action' so the recipient knows it's context-only without reading."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--type", "context",
            "test message",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    assert len(files) == 1
    assert "-action" not in files[0].name


def test_action_filename_with_from_suffix(tmp_path: Path) -> None:
    """When both --type action and --from are set, the filename includes
    both '-action' and '-from-<label>' suffixes, with type before from."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--from", "treadmill-alan",
            "--type", "action",
            "test message",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    assert len(files) == 1
    assert "-action" in files[0].name
    assert "-from-treadmill-alan" in files[0].name
    # Verify the order: type before from
    assert files[0].name.index("-action") < files[0].name.index("-from-")
