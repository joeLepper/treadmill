"""Tests for the pre-commit secret-leak hook.

No real literals — uses a SYNTHETIC denylist via a temp file, matching
the obsidian-gate test's approach.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the hook module importable (it lives one dir up).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pre_commit_secret_leak as hook  # noqa: E402


_DIFF = """\
diff --git a/docs/foo.md b/docs/foo.md
--- a/docs/foo.md
+++ b/docs/foo.md
@@ -0,0 +1,2 @@
+a clean line about widgets
+a line mentioning acme-client integration
diff --git a/src/bar.py b/src/bar.py
--- a/src/bar.py
+++ b/src/bar.py
@@ -1 +1 @@
-old benign line
+account 111122223333 hardcoded
"""


def test_added_lines_extracts_additions_with_paths() -> None:
    added = hook.added_lines(_DIFF)
    assert ("docs/foo.md", "a clean line about widgets") in added
    assert ("docs/foo.md", "a line mentioning acme-client integration") in added
    assert ("src/bar.py", "account 111122223333 hardcoded") in added
    # the '-old benign line' deletion and '+++ b/' headers are excluded
    assert all("old benign line" not in t for _, t in added)
    assert all(not t.startswith("+ b/") for _, t in added)


def test_find_hits_flags_denylist_tokens() -> None:
    added = hook.added_lines(_DIFF)
    hits = hook.find_hits(added, ["acme-client", "111122223333"])
    paths_tokens = {(p, tok) for p, tok, _ in hits}
    assert ("docs/foo.md", "acme-client") in paths_tokens
    assert ("src/bar.py", "111122223333") in paths_tokens


def test_find_hits_clean_when_no_match() -> None:
    added = [("docs/foo.md", "wholly benign content about widgets")]
    assert hook.find_hits(added, ["acme-client", "111122223333"]) == []


def test_find_hits_ignores_empty_denylist_token() -> None:
    added = [("docs/foo.md", "anything at all")]
    # an empty token would substring-match everything; must be ignored
    assert hook.find_hits(added, [""]) == []


def test_load_denylist_reads_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "codenames.json"
    p.write_text(json.dumps({"denylist": ["acme-client", "111122223333"]}))
    monkeypatch.setenv("TREADMILL_CODENAMES_FILE", str(p))
    assert hook.load_denylist() == ["acme-client", "111122223333"]


def test_load_denylist_missing_file_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TREADMILL_CODENAMES_FILE", str(tmp_path / "nope.json"))
    assert hook.load_denylist() == []


def test_load_denylist_unparseable_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("TREADMILL_CODENAMES_FILE", str(bad))
    assert hook.load_denylist() == []
