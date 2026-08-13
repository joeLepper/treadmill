"""Static no-hot-load scanner (ADR-0096 falsifier).

Scans a source tree for patterns that would introduce runtime hot-loading.
An empty result confirms the framework ships no hot-load path.

Banned patterns (per ADR-0096):
  - importlib.reload(...)
  - imp.reload(...)
  - exec( applied to source text
  - monkeypatching a running service via sys.modules assignment

Tests and this scanner itself are excluded from results.
"""

from __future__ import annotations

import re
from pathlib import Path

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("importlib.reload", re.compile(r"\bimportlib\.reload\s*\(")),
    ("imp.reload", re.compile(r"\bimp\.reload\s*\(")),
    ("exec(", re.compile(r"\bexec\s*\(")),
    ("sys.modules monkeypatch", re.compile(r"\bsys\.modules\s*\[.*\]\s*=")),
]

_EXCLUDE_DIRS = {"__pycache__", "tests", "test"}


def scan_for_hotload(root: Path) -> list[tuple[str, int]]:
    """Return (file_relative_to_root, line_number) for each hot-load pattern found.

    Excludes test directories and this file itself.
    """
    this_file = Path(__file__).resolve()
    findings: list[tuple[str, int]] = []

    for py_file in sorted(root.rglob("*.py")):
        if py_file.resolve() == this_file:
            continue
        if any(part in _EXCLUDE_DIRS for part in py_file.parts):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(source.splitlines(), start=1):
            for _name, pattern in _PATTERNS:
                if pattern.search(line):
                    rel = str(py_file.relative_to(root))
                    findings.append((rel, lineno))
                    break

    return findings
