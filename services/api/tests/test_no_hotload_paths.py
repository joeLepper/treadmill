"""ADR-0096 falsifier: the framework ships no hot-load path.

Scans services/api/treadmill_api for banned hot-load patterns.  If this test
fails, it names the offending file:line — that is a real ADR-0096 finding to
surface, not a test to weaken.
"""

from __future__ import annotations

from pathlib import Path

from treadmill_api.release.hotload_scan import scan_for_hotload

_FRAMEWORK_ROOT = Path(__file__).parent.parent / "treadmill_api"


def test_framework_ships_no_hotload_path() -> None:
    findings = scan_for_hotload(_FRAMEWORK_ROOT)
    if findings:
        lines = "\n".join(f"  {f}:{ln}" for f, ln in findings)
        raise AssertionError(
            f"ADR-0096 violation: hot-load patterns found in framework tree:\n{lines}"
        )
