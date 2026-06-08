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


def test_truncation_at_max_len(tmp_path: Path) -> None:
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", "treadmill-carla", "x" * (cc_relay.MAX_LEN + 1000)]
        cc_relay.main()

    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert len(content) == cc_relay.MAX_LEN
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


def test_context_type_no_header(tmp_path: Path) -> None:
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


def test_action_type_adds_header(tmp_path: Path) -> None:
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
            "x" * (cc_relay.MAX_LEN + 1000),
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert len(content) == cc_relay.MAX_LEN
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


# ── Coordinator features (ADR-0084 Task 1A) ────────────────────────────────


def test_to_many_writes_one_file_per_target(tmp_path: Path) -> None:
    """--to-many splits on comma and drops one file into each named target's
    inbox. No atomic broadcast — independent file writes."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to-many", "bert,carla,donna",
            "broadcast body",
        ]
        cc_relay.main()
    for label in ("bert", "carla", "donna"):
        files = list((tmp_path / ".cc-channels" / label / "relay").glob("*.md"))
        assert len(files) == 1, f"missing file for {label}"
        assert files[0].read_text() == "broadcast body"


def test_to_many_strips_whitespace_around_labels(tmp_path: Path) -> None:
    """--to-many tolerates spaces around labels: 'a, b , c' → ['a','b','c']."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to-many", "alan , bert,  carla ",
            "body",
        ]
        cc_relay.main()
    for label in ("alan", "bert", "carla"):
        assert (tmp_path / ".cc-channels" / label / "relay").is_dir()


def test_to_and_to_many_mutually_exclusive(tmp_path: Path) -> None:
    """Passing both --to and --to-many is a user error — exit before any
    inbox write so we don't silently send to a subset."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "carla",
            "--to-many", "bert,donna",
            "body",
        ]
        with pytest.raises(SystemExit):
            cc_relay.main()


def test_neither_to_nor_to_many_errors(tmp_path: Path) -> None:
    """At least one of --to / --to-many is required."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "body"]
        with pytest.raises(SystemExit):
            cc_relay.main()


def test_to_many_empty_after_strip_errors(tmp_path: Path) -> None:
    """--to-many with only commas/whitespace parses to no targets — error
    rather than silently doing nothing."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to-many", " , , ", "body"]
        with pytest.raises(SystemExit):
            cc_relay.main()


def test_subfolder_coord_routes_to_coord_subdir(tmp_path: Path) -> None:
    """--subfolder coord lands the file in relay/coord/ instead of relay/."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--subfolder", "coord",
            "coordinator brief",
        ]
        cc_relay.main()
    coord_files = list(
        (tmp_path / ".cc-channels" / "treadmill-carla" / "relay" / "coord").glob("*.md")
    )
    root_files = list(
        (tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md")
    )
    assert len(coord_files) == 1
    assert coord_files[0].read_text() == "coordinator brief"
    assert root_files == []


def test_subfolder_worker_routes_to_worker_subdir(tmp_path: Path) -> None:
    """--subfolder worker lands the file in relay/worker/ instead of relay/."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--subfolder", "worker",
            "worker assignment",
        ]
        cc_relay.main()
    worker_files = list(
        (tmp_path / ".cc-channels" / "treadmill-carla" / "relay" / "worker").glob("*.md")
    )
    assert len(worker_files) == 1
    assert worker_files[0].read_text() == "worker assignment"


def test_subfolder_creates_subdir_if_absent(tmp_path: Path) -> None:
    """The subfolder is created on demand — receivers don't pre-create it."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-newbie",
            "--subfolder", "coord",
            "first message",
        ]
        cc_relay.main()
    assert (tmp_path / ".cc-channels" / "treadmill-newbie" / "relay" / "coord").is_dir()


def test_invalid_subfolder_rejected(tmp_path: Path) -> None:
    """argparse choices= rejects unknown subfolder values so a typo doesn't
    silently fall back to relay/ root or create an arbitrary subdir."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--subfolder", "admin",  # not in ALLOWED_SUBFOLDERS
            "body",
        ]
        with pytest.raises(SystemExit):
            cc_relay.main()


def test_subfolder_with_to_many_routes_each(tmp_path: Path) -> None:
    """--subfolder applies to every target in a --to-many broadcast."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to-many", "bert,carla",
            "--subfolder", "coord",
            "broadcast to coord inboxes",
        ]
        cc_relay.main()
    for label in ("bert", "carla"):
        coord_files = list(
            (tmp_path / ".cc-channels" / label / "relay" / "coord").glob("*.md")
        )
        assert len(coord_files) == 1


def test_meta_adds_yaml_frontmatter(tmp_path: Path) -> None:
    """--meta key=val emits a YAML frontmatter block at the top of the body,
    delimited by '---' lines."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--meta", "plan_id=p-123",
            "--meta", "task_id=t-04",
            "body content",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert content.startswith("---\n")
    assert "plan_id: p-123\n" in content
    assert "task_id: t-04\n" in content
    # closing '---' followed by blank line, then body
    assert "\n---\n\nbody content" in content


def test_meta_repeatable_preserves_order(tmp_path: Path) -> None:
    """Multiple --meta flags accumulate in CLI order — the receiver can rely
    on insertion order for fields where order conveys meaning (e.g. priority
    flags)."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--meta", "alpha=1",
            "--meta", "bravo=2",
            "--meta", "charlie=3",
            "body",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert content.index("alpha: 1") < content.index("bravo: 2") < content.index("charlie: 3")


def test_meta_value_can_contain_equals(tmp_path: Path) -> None:
    """Split on FIRST '=' so values containing '=' (URLs, equations) survive."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--meta", "url=https://example.com/path?q=foo&r=bar",
            "body",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert "url: https://example.com/path?q=foo&r=bar\n" in content


def test_meta_without_equals_errors(tmp_path: Path) -> None:
    """--meta value missing '=' is a user error — exit so the receiver
    doesn't get malformed frontmatter."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--meta", "key_without_equals",
            "body",
        ]
        with pytest.raises(SystemExit):
            cc_relay.main()


def test_meta_empty_key_errors(tmp_path: Path) -> None:
    """A '=val' pair has no key — error rather than emit invalid YAML."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--meta", "=orphan",
            "body",
        ]
        with pytest.raises(SystemExit):
            cc_relay.main()


def test_meta_precedes_action_header(tmp_path: Path) -> None:
    """When --type action and --meta are both set, frontmatter lands at the
    very top (YAML convention); the action header still occupies the first
    line of the POST-frontmatter body so a receiver that strips frontmatter
    sees the header on line 1."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--type", "action",
            "--meta", "plan_id=p-1",
            "action body",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert content.startswith("---\n")
    # closing '---' for frontmatter, blank line, action header on next non-blank line
    assert "\n---\n\n[ACTION REQUEST]\n" in content
    # Frontmatter strictly precedes the header in the body
    assert content.index("---") < content.index(cc_relay.ACTION_HEADER)


def test_meta_with_from_label_order(tmp_path: Path) -> None:
    """Full assembly: frontmatter → action header → from-prefix → body."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = [
            "cc-relay.py",
            "--to", "treadmill-carla",
            "--from", "treadmill-alan",
            "--type", "action",
            "--meta", "priority=high",
            "do the thing",
        ]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    # Verify ordering: frontmatter < action header < [from:] < body
    assert content.index("priority: high") < content.index(cc_relay.ACTION_HEADER)
    assert content.index(cc_relay.ACTION_HEADER) < content.index("[from: treadmill-alan]")
    assert content.index("[from: treadmill-alan]") < content.index("do the thing")


def test_no_meta_no_frontmatter_emitted(tmp_path: Path) -> None:
    """Without --meta the file looks exactly like a pre-Task-1A relay file —
    no '---' lines anywhere. Backward compatibility with existing receivers."""
    with patch("cc_relay.Path.home", return_value=tmp_path):
        sys.argv = ["cc-relay.py", "--to", "treadmill-carla", "plain body"]
        cc_relay.main()
    files = list((tmp_path / ".cc-channels" / "treadmill-carla" / "relay").glob("*.md"))
    content = files[0].read_text()
    assert content == "plain body"
    assert "---" not in content
