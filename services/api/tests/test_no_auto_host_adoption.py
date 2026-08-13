"""Static falsifier — no code path relocates an agent because its host is unreachable.

Motivation (ADR-0095, plan 3017bf41): the 2026-08-11 outage was caused by
exec_otp's automatic host-adoption. This test is the standing guard that
Treadmill never grows an equivalent path.

The guard is a repo grep — no Postgres, no network, no Docker required.

Tests:
  test_no_auto_adopt_on_host_unreachable — the real guard. Fails with file+line.
  test_falsifier_can_fail                — meta-test for Check 1 (banned identifiers).
  test_check2_flags_real_relocation      — meta-test for Check 2 positive case.
  test_check2_does_not_flag_disclaiming_docstring — meta-test for Check 2 negative case.

Check 2 design note (v2, evaluator rework b904e1a0):
  The original Check 2 matched trigger+action words over the raw function text,
  including docstrings. This produced a false positive on host_registry_store.py
  ::bind_label whose docstring reads "no caller may rebind automatically on
  host-unreachable" — correctly documentary, not executable. The fix: strip the
  docstring and comment-only lines from the function text before matching.
  See _executable_func_text().
"""

from __future__ import annotations

import ast
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

# ── Documented pattern constants ──────────────────────────────────────────────
# Extend these lists as new auto-adoption shapes emerge.
# Removing an entry weakens the guard — do not remove without an ADR update.

# Check 1: any occurrence of these identifiers in non-test, non-docs source
# is a violation, regardless of context.
BANNED_IDENTIFIERS: list[str] = [
    "adopt_on_unreachable",
    "reassign_on_unreachable",
    "relocate_on_unreachable",
    "auto_migrate",
    "steal_labels",
    "reconcile_hosts",
    "rebalance_labels",
]

# Check 2: a Python function whose EXECUTABLE body (docstring and comment-only
# lines stripped) contains both a trigger phrase AND an action phrase is a
# violation. The pair models "detect host-unreachable → automatically move agent".
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

# Approved exceptions for Check 2.
# Key:   "<file_stem>:<function_name>"   (e.g. "host_registry_store:bind_label")
# Value: explanation for the exemption.
# Keep this list short — each entry is a permanent exemption that weakens the
# guard. Prefer fixing the code over adding exceptions.
APPROVED_CO_OCCURRENCE_EXCEPTIONS: dict[str, str] = {}

# Directories excluded from all scans (matched against relative path components).
_EXCLUDED_DIRS: frozenset[str] = frozenset({"docs", "tests", ".git"})


# ── Core scan helpers ─────────────────────────────────────────────────────────


def _should_exclude(path: Path, root: Path, this_file: Path) -> bool:
    """Return True when *path* should be skipped by the scan."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    if any(p in _EXCLUDED_DIRS for p in parts):
        return True
    if path.resolve() == this_file.resolve():
        return True
    return False


def _executable_func_text(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> str:
    """Return the function body text with docstring and comment-only lines removed.

    Matching against raw function text (including docstrings) produces false
    positives on functions that DISCLAIM automatic relocation — e.g.
    ``bind_label``'s docstring reads "no caller may rebind automatically on
    host-unreachable". That text is documentary, not executable. Stripping the
    docstring node and comment-only lines before matching eliminates that class
    of false positive while preserving detection of real violations in
    executable code.
    """
    body = node.body

    # Skip the docstring: the first statement when it is a bare string constant.
    skip = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        skip = 1

    effective = body[skip:]
    if not effective:
        return ""

    start = effective[0].lineno - 1  # 0-indexed
    end = effective[-1].end_lineno or len(source_lines)
    lines = source_lines[start:end]
    # Drop comment-only lines. Inline trailing comments (e.g. `x = f()  # note`)
    # ride their actual-code line and are intentionally kept.
    lines = [ln for ln in lines if not ln.strip().startswith("#")]
    return "\n".join(lines).lower()


# ── Core scan logic (shared by all tests) ────────────────────────────────────


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
            if launcher.is_file() and launcher.suffix != ".py":
                if not _should_exclude(launcher, root, this_file):
                    files_to_scan.append(launcher)

    for filepath in files_to_scan:
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # --- Check 1: banned identifiers (all file types) ---
        for identifier in BANNED_IDENTIFIERS:
            pattern = re.compile(r"\b" + re.escape(identifier) + r"\b")
            for match in pattern.finditer(source):
                line_no = source[: match.start()].count("\n") + 1
                violations.append(
                    (str(filepath), line_no, f"banned identifier {identifier!r}")
                )

        # --- Check 2: co-occurrence in Python function EXECUTABLE body ---
        # Docstrings and comment-only lines are excluded before matching.
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

            exec_text = _executable_func_text(node, source_lines)
            has_trigger = any(t in exec_text for t in TRIGGER_PHRASES)
            has_action = any(a in exec_text for a in ACTION_PHRASES)

            if not (has_trigger and has_action):
                continue

            exception_key = f"{filepath.stem}:{node.name}"
            if exception_key in APPROVED_CO_OCCURRENCE_EXCEPTIONS:
                continue

            violations.append(
                (
                    str(filepath),
                    node.lineno,
                    f"function {node.name!r} contains both a host-unreachable "
                    f"trigger and a relocation/adoption action in executable code — "
                    f"if legitimate, add {exception_key!r} to "
                    f"APPROVED_CO_OCCURRENCE_EXCEPTIONS with an explanation",
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
    agents from an unreachable host. ADR-0095 prohibits this pattern: an
    agent moves hosts ONLY by explicit operator or coordinator decision.
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
    """Meta-test for Check 1: prove the banned-identifier scan is not vacuous.

    Creates a temp file containing adopt_on_unreachable, runs scan_tree on a
    tree that includes it, and asserts the violation is reported.
    """
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

    sentinel = tmp_path / "nonexistent_this_file.py"
    violations = scan_tree(tmp_path, this_file=sentinel)

    assert violations, (
        "scan_tree returned no violations for a file containing 'adopt_on_unreachable'. "
        "The Check-1 falsifier is broken."
    )
    violating_files = {v[0] for v in violations}
    assert str(fake_src) in violating_files, (
        f"Expected {fake_src} to be flagged; got {violating_files}"
    )


def test_check2_flags_real_relocation(tmp_path: Path) -> None:
    """Meta-test for Check 2 positive case: executable relocation code IS flagged.

    A function whose body (not just its docstring) contains both a trigger phrase
    and an action phrase must be reported as a violation.
    """
    fake_src = tmp_path / "src" / "scheduler.py"
    fake_src.parent.mkdir(parents=True)
    fake_src.write_text(
        textwrap.dedent(
            """\
            async def handle_heartbeat_timeout(host, label):
                if host.unreachable:
                    await rebind(label, fallback_host)
            """
        )
    )

    sentinel = tmp_path / "nonexistent_this_file.py"
    violations = scan_tree(tmp_path, this_file=sentinel)

    co_occurrence = [v for v in violations if "function" in v[2]]
    assert co_occurrence, (
        "Check 2 did not flag a function whose executable body contains "
        "both 'unreachable' (trigger) and 'rebind' (action). "
        "The co-occurrence guard is broken."
    )
    assert str(fake_src) in {v[0] for v in co_occurrence}, (
        f"Expected {fake_src} in flagged files; got {co_occurrence}"
    )


def test_check2_does_not_flag_disclaiming_docstring(tmp_path: Path) -> None:
    """Meta-test for Check 2 negative case: a disclaiming docstring is NOT flagged.

    host_registry_store.py::bind_label carries the docstring:
        "no caller may rebind automatically on host-unreachable"
    Both 'rebind' and 'unreachable' appear there, but only in the docstring.
    Check 2 must not report this as a violation.

    This test uses the same code shape as the real bind_label to confirm the fix.
    """
    fake_src = tmp_path / "src" / "host_registry_store.py"
    fake_src.parent.mkdir(parents=True)
    fake_src.write_text(
        textwrap.dedent(
            '''\
            async def bind_label(session, label, host, bound_by=None):
                """Upsert the label→host binding. This is the ONLY write path.

                Explicit writes only — no caller may rebind automatically on
                host-unreachable (ADR-0095 invariant).
                """
                stmt = insert(SessionHostBinding).values(label=label, host=host)
                await session.execute(stmt)
            '''
        )
    )

    sentinel = tmp_path / "nonexistent_this_file.py"
    violations = scan_tree(tmp_path, this_file=sentinel)

    co_occurrence = [v for v in violations if "function" in v[2]]
    assert not co_occurrence, (
        f"Check 2 falsely flagged a disclaiming docstring as a violation: "
        f"{co_occurrence}. The docstring says we do NOT rebind on unreachable — "
        f"that is correct documentation, not a forbidden code path."
    )
