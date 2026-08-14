"""Static falsifier — no code path reroutes/relocates placement on load or reachability.

ADR-0100 Decision 4 + ADR-0098: placement is set once at start; nothing schedules,
load-balances, or reroutes it afterward. This is the standing guard.

ADR-0100 §Risks: "A fail-closed refusal of an unreachable directed host (Decision 4)
is not a reroute and is not a violation; the falsifier scopes to relocate/reroute, never
to refuse." The scan is designed to flag active rerouting/load-balancing, not refusals.

Modelled on test_no_auto_host_adoption.py (ADR-0095 guard, #379/#382) — same two-check
structure and the _executable_func_text helper that strips docstrings.

Tests:
  test_no_reroute_placement          — real guard. Fails with file+line on violation.
  test_falsifier_check1_can_fail     — meta-test for Check 1 (banned identifiers).
  test_falsifier_check2_flags_load_reroute — meta-test for Check 2 positive case.
  test_falsifier_check2_not_flagged_for_refusal — meta-test: refusal code is NOT flagged.
"""

from __future__ import annotations

import ast
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

# ── Documented pattern constants ──────────────────────────────────────────────
# Extend these lists as new reroute/load-balance shapes emerge.
# Removing an entry weakens the guard — do not remove without an ADR update.

# Check 1: these identifiers in non-test, non-docs source are violations, regardless
# of context. They name code paths the ADR explicitly forbids.
BANNED_IDENTIFIERS: list[str] = [
    "auto_reroute",
    "reroute_on_load",
    "relocate_on_load",
    "fallback_placement",
    "load_balance_placement",
    "rebalance_placement",
    "redirect_placement",
]

# Check 2: a Python function whose EXECUTABLE body (docstring and comment-only
# lines stripped) contains both a LOAD trigger phrase AND a reroute action phrase
# is a violation. The pair models "detect overloaded/saturated host → redirect agent".
# Scope: LOAD-triggered rerouting only. Reachability-triggered refusal (placement.py's
# fail-closed return) is not flagged because this scan uses load triggers, not
# unreachable triggers — a refusal on an unreachable host is ADR-0100 Decision 4
# and is explicitly NOT a violation.
LOAD_TRIGGER_PHRASES: list[str] = [
    "overloaded",
    "load_exceeded",
    "at_capacity",
    "host_load",
    "load_threshold",
    "capacity_exceeded",
]

REROUTE_ACTION_PHRASES: list[str] = [
    "reroute",
    "redirect",
    "fallback",
]

# Approved exceptions for Check 2.
# Key:   "<file_stem>:<function_name>"
# Value: explanation for the exemption.
# Keep this list short — each entry permanently weakens the guard.
APPROVED_CO_OCCURRENCE_EXCEPTIONS: dict[str, str] = {}

# Directories excluded from all scans (matched against relative path components).
_EXCLUDED_DIRS: frozenset[str] = frozenset({"docs", "tests", ".git"})


# ── Core scan helpers (same structure as test_no_auto_host_adoption.py) ────────


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
    """Return function body text with docstring and comment-only lines removed.

    A function that DISCLAIMS rerouting in its docstring (e.g.
    "never reroutes to a fallback host") must not be flagged — the phrase
    is documentary, not executable. Stripping the docstring node and
    comment-only lines before matching eliminates that false-positive class
    while preserving detection of real violations in executable code.

    Provenance: identical logic to test_no_auto_host_adoption._executable_func_text;
    kept in sync manually so both guards apply the same stripping semantics.
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
    lines = [ln for ln in lines if not ln.strip().startswith("#")]
    return "\n".join(lines).lower()


# ── Core scan logic ────────────────────────────────────────────────────────────


def scan_tree(root: Path, this_file: Path | None = None) -> list[tuple[str, int, str]]:
    """Walk *root* and return all placement-reroute violations.

    Each violation is a ``(file_path, line_number, description)`` triple.
    An empty result means the tree is clean.
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

        # --- Check 2: load-trigger + reroute-action co-occurrence in executable body ---
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
            has_load_trigger = any(t in exec_text for t in LOAD_TRIGGER_PHRASES)
            has_reroute_action = any(a in exec_text for a in REROUTE_ACTION_PHRASES)

            if not (has_load_trigger and has_reroute_action):
                continue

            exception_key = f"{filepath.stem}:{node.name}"
            if exception_key in APPROVED_CO_OCCURRENCE_EXCEPTIONS:
                continue

            violations.append(
                (
                    str(filepath),
                    node.lineno,
                    f"function {node.name!r} contains both a host-load trigger "
                    f"and a reroute/redirect/fallback action in executable code — "
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


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_no_reroute_placement() -> None:
    """Assert no code path reroutes or load-balances agent placement.

    ADR-0100 Decision 4: if a directed host is unregistered or unreachable, the
    start is refused and surfaced — never silently rerouted to another host.
    ADR-0100 Decision 3: nothing allocates agents across a pool; nothing relocates
    an agent because its host is under load. Placement is set once at start.
    """
    root = _repo_root()
    violations = scan_tree(root)

    if violations:
        lines = [
            "VIOLATION: placement-reroute / load-balance pattern found.",
            "ADR-0100 requires that placement is set at start and never rerouted.",
            f"  {len(violations)} violation(s):",
        ]
        for file_path, line_no, desc in violations:
            lines.append(f"    {file_path}:{line_no}  {desc}")
        pytest.fail("\n".join(lines))


def test_falsifier_check1_can_fail(tmp_path: Path) -> None:
    """Meta-test for Check 1: prove the banned-identifier scan is non-vacuous.

    Plants 'auto_reroute' in a temp source file, runs scan_tree on a tree
    containing it, and asserts the violation is reported.
    """
    fake_src = tmp_path / "src" / "bad_scheduler.py"
    fake_src.parent.mkdir(parents=True)
    fake_src.write_text(
        textwrap.dedent(
            """\
            async def handle_placement(label, host):
                if host_overloaded(host):
                    auto_reroute(label, host)
            """
        )
    )

    sentinel = tmp_path / "nonexistent_this_file.py"
    violations = scan_tree(tmp_path, this_file=sentinel)

    assert violations, (
        "scan_tree returned no violations for a file containing 'auto_reroute'. "
        "The Check-1 falsifier is broken."
    )
    violating_files = {v[0] for v in violations}
    assert str(fake_src) in violating_files, (
        f"Expected {fake_src} to be flagged; got {violating_files}"
    )


def test_falsifier_check2_flags_load_reroute(tmp_path: Path) -> None:
    """Meta-test for Check 2 positive case: load-reroute in executable code IS flagged.

    A function whose executable body contains both a load trigger ('overloaded')
    and a reroute action ('redirect') must be reported as a violation.
    """
    fake_src = tmp_path / "src" / "load_balancer.py"
    fake_src.parent.mkdir(parents=True)
    fake_src.write_text(
        textwrap.dedent(
            """\
            async def handle_agent_placement(label, host):
                if host_overloaded(host):
                    await redirect(label, pick_another_host())
            """
        )
    )

    sentinel = tmp_path / "nonexistent_this_file.py"
    violations = scan_tree(tmp_path, this_file=sentinel)

    co_occurrence = [v for v in violations if "function" in v[2]]
    assert co_occurrence, (
        "Check 2 did not flag a function whose executable body contains "
        "both 'overloaded' (load trigger) and 'redirect' (reroute action). "
        "The load-reroute co-occurrence guard is broken."
    )
    assert str(fake_src) in {v[0] for v in co_occurrence}, (
        f"Expected {fake_src} in flagged files; got {co_occurrence}"
    )


def test_falsifier_check2_not_flagged_for_refusal(tmp_path: Path) -> None:
    """Meta-test: fail-closed refusal code is NOT flagged by Check 2.

    placement.py returns a failure result when a directed host is unreachable —
    it does NOT reroute. The scan uses load triggers, not 'unreachable', so the
    refusal path does not match Check 2. This test verifies that shape is safe.

    ADR-0100 §Risks: "A fail-closed refusal is not a reroute and is not a violation."
    """
    fake_src = tmp_path / "src" / "placement.py"
    fake_src.parent.mkdir(parents=True)
    # This shape mirrors placement.py's actual refusal — unreachable check, no load terms.
    fake_src.write_text(
        textwrap.dedent(
            '''\
            async def place_agent(session, label, store, directed_host=None):
                """Place an agent. Refuses if directed host is unreachable."""
                hosts = await store.list_hosts(session)
                host_map = {h.name: h for h in hosts}
                if directed_host not in host_map:
                    return PlacementResult(success=False, reason="not registered")
                if host_map[directed_host].tailnet_addr is None:
                    return PlacementResult(success=False, reason="no tailnet addr")
                await store.bind_label(session, label=label, host=directed_host)
                return PlacementResult(success=True, host=directed_host)
            '''
        )
    )

    sentinel = tmp_path / "nonexistent_this_file.py"
    violations = scan_tree(tmp_path, this_file=sentinel)

    co_occurrence = [v for v in violations if "function" in v[2]]
    assert not co_occurrence, (
        f"Check 2 falsely flagged fail-closed refusal code as a reroute violation: "
        f"{co_occurrence}. A refusal is not a reroute (ADR-0100 §Risks)."
    )
