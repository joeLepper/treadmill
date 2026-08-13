"""Static falsifier — no code path relocates an agent because its host is unreachable.

Motivation (ADR-0095, plan 3017bf41):  the 2026-08-11 outage was caused by
exec_otp's automatic host-adoption.  This test is the standing guard that
Treadmill never grows an equivalent path.

The guard is a repo grep — no Postgres, no network, no Docker required.

Two tests:
  test_no_auto_adopt_on_host_unreachable — the real guard.  Fails loudly
      with file+line if a violation is found.
  test_falsifier_can_fail — meta-test.  Proves the scan is not vacuous:
      constructs a temp file that matches a banned pattern, runs the same
      scan function against a tree that contains it, and asserts the scan
      reports it.  Prevents the "green-but-asserts-nothing" failure mode.
"""

from __future__ import annotations

import ast
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

# ── Documented pattern constants ──────────────────────────────────────────────
# Add to these lists when new auto-adoption shapes emerge.  Removing one
# weakens the guard — do not remove without a corresponding ADR update.

# Any occurrence of these identifiers in non-test, non-docs source is a
# violation, regardless of context.
BANNED_IDENTIFIERS: list[str] = [
    "adopt_on_unreachable",
    "reassign_on_unreachable",
    "relocate_on_unreachable",
    "auto_migrate",
    "steal_labels",
    "reconcile_hosts",
    "rebalance_labels",
]

# Co-occurrence check: a Python function whose body contains BOTH a trigger
# phrase AND an action phrase is a violation.  The pair models the shape
# "detect host-unreachable → automatically move the agent".
TRIGGER_PHRASES: list[str] = [
    "unreachable",
    "host_down",
    "host down",
    "heartbeat_timeout",
    "heartbeat timeout",
]

ACTION_PHRASES: list[str] = [
    "rebind",
    "relocate",
    "reassign",
    "adopt",
]

# Directories excluded from all scans (relative path component match).
_EXCLUDED_DIRS: frozenset[str] = frozenset({"docs", "tests", ".git"})


# ── Core scan logic (shared by both tests) ────────────────────────────────────


def _should_exclude(path: Path, root: Path, this_file: Path) -> bool:
    """Return True when *path* should be skipped."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    if any(p in _EXCLUDED_DIRS for p in parts):
        return True
    if path.resolve() == this_file.resolve():
        return True
    return False


def scan_tree(root: Path, this_file: Path | None = None) -> list[tuple[str, int, str]]:
    """Walk *root* and return all auto-adoption violations.

    Each violation is a ``(file_path, line_number, description)`` triple.
    An empty result means the tree is clean.

    Files scanned:
    - ``*.py`` under *root* (excluding docs/, tests/, .git/, this test)
    - All files under ``root/tools/cc-channels/`` (shell launchers)
    """
    if this_file is None:
        this_file = Path(__file__).resolve()

    violations: list[tuple[str, int, str]] = []
    files_to_scan: list[Path] = []

    for py_file in sorted(root.rglob("*.py")):
        if not _should_exclude(py_file, root, this_file):
            files_to_scan.append(py_file)

    cc_channels = root / "tools" / "cc-channels"
    if cc_channels.exists():
        for launcher in sorted(cc_channels.rglob("*")):
            if launcher.is_file() and not launcher.suffix == ".py":
                if not _should_exclude(launcher, root, this_file):
                    files_to_scan.append(launcher)

    for filepath in files_to_scan:
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Check 1 — banned identifiers (applies to all file types).
        for identifier in BANNED_IDENTIFIERS:
            pattern = re.compile(r"\b" + re.escape(identifier) + r"\b")
            for match in pattern.finditer(source):
                line_no = source[: match.start()].count("\n") + 1
                violations.append(
                    (str(filepath), line_no, f"banned identifier {identifier!r}")
                )

        # Check 2 — co-occurrence within a Python function body.
        if filepath.suffix != ".py":
            continue
        try:
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            continue

        source_lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start = node.lineno - 1
            end = node.end_lineno if node.end_lineno is not None else len(source_lines)
            func_text = "\n".join(source_lines[start:end]).lower()

            has_trigger = any(trigger in func_text for trigger in TRIGGER_PHRASES)
            has_action = any(action in func_text for action in ACTION_PHRASES)

            if has_trigger and has_action:
                violations.append(
                    (
                        str(filepath),
                        node.lineno,
                        f"function {node.name!r} contains both a host-unreachable "
                        f"trigger and a relocation/adoption action — if this is "
                        f"legitimate, add it to the approved exceptions list in this "
                        f"test with an explanation",
                    )
                )

    return violations


def _repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_no_auto_adopt_on_host_unreachable() -> None:
    """Assert the repo contains no automatic host-adoption/relocation path.

    The 2026-08-11 outage was caused by exec_otp's automatic adoption of
    agents from an unreachable host.  ADR-0095 prohibits this pattern:
    an agent moves hosts ONLY by explicit operator or coordinator decision.

    This test is the committed Check for that invariant.  If it fails,
    the offending location is printed so the reviewer can assess whether
    the code is a real violation or a false positive that warrants adding
    an approved-exceptions entry here.
    """
    root = _repo_root()
    violations = scan_tree(root)

    if violations:
        lines = [
            "VIOLATION: auto-adoption / host-relocation pattern found.",
            "ADR-0095 requires that an agent moves hosts only by explicit decision.",
            f"  {len(violations)} violation(s):",
        ]
        for file_path, line_no, desc in violations:
            lines.append(f"    {file_path}:{line_no}  {desc}")
        pytest.fail("\n".join(lines))


def test_falsifier_can_fail(tmp_path: Path) -> None:
    """Meta-test: prove the scan is not vacuous.

    Creates a temp file whose content matches a banned pattern, runs the
    real scan_tree function against a tree that includes it, and asserts
    a violation is reported.  A scan that always returns empty would make
    the main test green regardless of what is in the codebase.
    """
    # Create a fake source file with a banned identifier.
    fake_src = tmp_path / "src" / "bad_supervisor.py"
    fake_src.parent.mkdir(parents=True)
    fake_src.write_text(
        textwrap.dedent(
            """\
            def handle_host_event(host, label):
                if host.unreachable:
                    adopt_on_unreachable(label, host)
            """
        )
    )

    # Use a sentinel path that won't match the real test file.
    sentinel = tmp_path / "nonexistent_this_file.py"
    violations = scan_tree(tmp_path, this_file=sentinel)

    assert violations, (
        "scan_tree returned no violations for a file containing 'adopt_on_unreachable'. "
        "The falsifier is broken — the main test is no longer a meaningful guard."
    )

    # Confirm the violation points at the right file.
    violating_files = {v[0] for v in violations}
    assert str(fake_src) in violating_files, (
        f"Expected {fake_src} to be flagged; got {violating_files}"
    )
