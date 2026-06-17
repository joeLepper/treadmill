"""Tests for the CI secret-leak scan (server-side gate).

Synthetic denylist + diff (no real literals). Runs the script as a
subprocess feeding the diff on stdin + the denylist via env, mirroring
how the workflow invokes it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "ci_secret_leak_scan.py"

_DIFF = """\
diff --git a/docs/foo.md b/docs/foo.md
--- a/docs/foo.md
+++ b/docs/foo.md
@@ -0,0 +1,1 @@
+a line mentioning acme-client integration
"""

_CLEAN_DIFF = """\
diff --git a/docs/foo.md b/docs/foo.md
--- a/docs/foo.md
+++ b/docs/foo.md
@@ -0,0 +1,1 @@
+a wholly benign line about widgets
"""


def _run(diff: str, denylist: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=diff,
        env={"CODENAME_DENYLIST": denylist, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def test_hit_fails_and_names_the_file() -> None:
    r = _run(_DIFF, "acme-client\n111122223333\n")
    assert r.returncode == 1
    assert "docs/foo.md" in r.stderr


def test_hit_output_is_REDACTED_no_token_value() -> None:
    """Actions logs are public — the matched token must never be printed."""
    r = _run(_DIFF, "acme-client\n111122223333\n")
    assert "acme-client" not in (r.stdout + r.stderr)
    assert "111122223333" not in (r.stdout + r.stderr)


def test_clean_diff_passes() -> None:
    r = _run(_CLEAN_DIFF, "acme-client\n111122223333\n")
    assert r.returncode == 0


def test_empty_denylist_skips_pass() -> None:
    """Fork PRs don't receive the secret → empty denylist → no-op pass."""
    r = _run(_DIFF, "")
    assert r.returncode == 0
