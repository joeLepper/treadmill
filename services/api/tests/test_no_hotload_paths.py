"""ADR-0096 falsifier: the framework ships no hot-load path.

Two tests:
  test_framework_ships_no_hotload_path — the standing guard. Scans
      services/api/treadmill_api for banned hot-load patterns. If it fails,
      the offending file:line is a real ADR-0096 finding to surface, not a
      test to weaken.
  test_scan_for_hotload_reports_all_banned_patterns — meta-test. Plants each
      banned pattern in a temp tree, runs the same scanner, and asserts every
      pattern is reported. Prevents the green-but-asserts-nothing failure mode
      (the scanner could silently stop detecting a pattern after a refactor).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from treadmill_api.release.hotload_scan import scan_for_hotload

_FRAMEWORK_ROOT = Path(__file__).parent.parent / "treadmill_api"


def test_framework_ships_no_hotload_path() -> None:
    findings = scan_for_hotload(_FRAMEWORK_ROOT)
    if findings:
        lines = "\n".join(f"  {f}:{ln}" for f, ln in findings)
        raise AssertionError(
            f"ADR-0096 violation: hot-load patterns found in framework tree:\n{lines}"
        )


def test_scan_for_hotload_reports_all_banned_patterns(tmp_path: Path) -> None:
    """Meta-test: prove scan_for_hotload is non-vacuous for all four banned patterns.

    Plants one instance of each banned pattern in a temp source file, runs
    scan_for_hotload against the temp tree, and asserts every planted line is
    reported. A scanner that misses any pattern would make the standing guard
    meaningless against a future refactor that silently removes detection.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    bad = src_dir / "bad_module.py"

    # Each banned pattern is on a known line so we can assert it by lineno.
    # Line numbers are 1-based, matching what scan_for_hotload returns.
    source = (
        "import importlib\n"          # 1
        "import imp\n"                 # 2
        "import sys\n"                 # 3
        "\n"                           # 4
        "importlib.reload(some_mod)\n" # 5 — banned: importlib.reload
        "imp.reload(other_mod)\n"      # 6 — banned: imp.reload
        "exec(source_code)\n"          # 7 — banned: exec(
        "sys.modules['foo'] = bar\n"   # 8 — banned: sys.modules monkeypatch
    )
    bad.write_text(source, encoding="utf-8")

    findings = scan_for_hotload(tmp_path)
    found_lines = {lineno for _, lineno in findings}

    assert 5 in found_lines, (
        "scan_for_hotload did not detect 'importlib.reload(' on line 5 — "
        "the scanner no longer catches this banned pattern"
    )
    assert 6 in found_lines, (
        "scan_for_hotload did not detect 'imp.reload(' on line 6 — "
        "the scanner no longer catches this banned pattern"
    )
    assert 7 in found_lines, (
        "scan_for_hotload did not detect 'exec(' on line 7 — "
        "the scanner no longer catches this banned pattern"
    )
    assert 8 in found_lines, (
        "scan_for_hotload did not detect 'sys.modules[...] =' on line 8 — "
        "the scanner no longer catches this banned pattern"
    )
