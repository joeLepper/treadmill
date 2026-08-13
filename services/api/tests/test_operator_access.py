"""Tests for treadmill_api/operator_access/ (ADR-0097 operator-identity gate).

Behavioral tests cover acl.is_operator and bind.tailnet_bind_addr / funnel_enabled.
Static falsifier test scans the operator_access source for banned patterns
(wildcard bind literal, Funnel-enable pattern) — this IS the gate, so it must
be non-vacuous. The meta-test proves the scanner can detect a violation.
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from treadmill_api.operator_access.acl import is_operator
from treadmill_api.operator_access.bind import funnel_enabled, tailnet_bind_addr

# ── Behavioral: is_operator ───────────────────────────────────────────────────


def test_is_operator_exact_match_allowed() -> None:
    assert is_operator("joe@tailnet", operator_identity="joe@tailnet") is True


def test_is_operator_different_identity_denied() -> None:
    assert is_operator("attacker@tailnet", operator_identity="joe@tailnet") is False


def test_is_operator_empty_string_denied() -> None:
    assert is_operator("", operator_identity="joe@tailnet") is False


def test_is_operator_none_denied() -> None:
    assert is_operator(None, operator_identity="joe@tailnet") is False  # type: ignore[arg-type]


# ── Behavioral: tailnet_bind_addr ─────────────────────────────────────────────


def test_tailnet_bind_addr_cgnat_returned_unchanged() -> None:
    assert tailnet_bind_addr("100.101.102.103") == "100.101.102.103"


@pytest.mark.parametrize(
    "banned",
    [
        "0.0.0.0",          # wildcard / unspecified
        "",                  # empty string
        "127.0.0.1",        # loopback
        "localhost",         # loopback hostname (lowercase)
        "LOCALHOST",         # case-variant — old denylist was case-sensitive; allowlist closes it
        "192.168.1.50",     # LAN (RFC 1918 class C)
        "10.0.0.5",         # LAN (RFC 1918 class A)
        "8.8.8.8",          # public IP
        "example.com",      # arbitrary hostname — fail-closed, no passthrough
        "not-an-ip-host",   # non-IP hostname — fail-closed, no passthrough
    ],
)
def test_tailnet_bind_addr_banned_raises(banned: str) -> None:
    with pytest.raises(ValueError):
        tailnet_bind_addr(banned)


# ── Behavioral: funnel_enabled ────────────────────────────────────────────────


def test_funnel_enabled_is_false() -> None:
    assert funnel_enabled() is False


# ── Static falsifier ──────────────────────────────────────────────────────────

# Patterns that must NOT appear in operator_access/*.py source.
_WILDCARD_LITERAL = "0.0.0.0"
_FUNNEL_ENABLE_PATTERN = re.compile(r"funnel.*True", re.IGNORECASE)


def _scan_operator_access(root: Path) -> list[tuple[str, int, str]]:
    """Scan operator_access/*.py for banned patterns.

    Returns a list of (file_path, line_number, description) triples.
    An empty result means the package is clean.

    Banned patterns:
    - Wildcard bind literal "0.0.0.0" (decision 3: bind to Tailscale interface only)
    - Funnel-enable pattern (decision 4: Funnel is never enabled)
    """
    package = root / "services" / "api" / "treadmill_api" / "operator_access"
    violations: list[tuple[str, int, str]] = []

    for py_file in sorted(package.glob("*.py")):
        source = py_file.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(source.splitlines(), start=1):
            if _WILDCARD_LITERAL in line:
                violations.append(
                    (str(py_file), line_no, f"wildcard bind literal {_WILDCARD_LITERAL!r}")
                )
            if _FUNNEL_ENABLE_PATTERN.search(line):
                violations.append(
                    (str(py_file), line_no, "Funnel-enable pattern")
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


def test_operator_access_source_is_clean() -> None:
    """Assert operator_access/*.py contains no wildcard bind or Funnel-enable pattern.

    ADR-0097 §Decision 3 and 4: services bind to the Tailscale interface only;
    Funnel is never enabled. This static scan is the standing guard.
    """
    root = _repo_root()
    violations = _scan_operator_access(root)

    if violations:
        lines = [
            "VIOLATION: forbidden pattern in operator_access source (ADR-0097).",
            f"  {len(violations)} violation(s):",
        ]
        for file_path, line_no, desc in violations:
            lines.append(f"    {file_path}:{line_no}  {desc}")
        pytest.fail("\n".join(lines))


def test_wildcard_scan_detects_violation(tmp_path: Path) -> None:
    """Meta-test: prove the wildcard-bind scan is not vacuous.

    Plants '0.0.0.0' in a temp operator_access file and asserts the scanner
    flags it — so the gate proves it can fail, not just pass on an empty match.
    """
    pkg = tmp_path / "services" / "api" / "treadmill_api" / "operator_access"
    pkg.mkdir(parents=True)
    (pkg / "bad_bind.py").write_text(
        textwrap.dedent(
            """\
            # Accidentally hardcoded wildcard
            BIND_ADDR = "0.0.0.0"
            """
        )
    )

    violations = _scan_operator_access(tmp_path)

    assert violations, (
        "scan returned no violations for a file containing the wildcard bind literal. "
        "The wildcard-bind falsifier is broken."
    )
    assert any(_WILDCARD_LITERAL in desc for _, _, desc in violations), (
        f"Expected a wildcard violation; got: {violations}"
    )


def test_funnel_scan_detects_violation(tmp_path: Path) -> None:
    """Meta-test: prove the Funnel-enable scan is not vacuous.

    Plants a Funnel-enable pattern in a temp operator_access file and asserts
    the scanner flags it — symmetric non-vacuity check matching the wildcard gate.
    """
    pkg = tmp_path / "services" / "api" / "treadmill_api" / "operator_access"
    pkg.mkdir(parents=True)
    (pkg / "bad_funnel.py").write_text(
        textwrap.dedent(
            """\
            # Accidentally enabled Funnel
            funnel_enabled = True
            """
        )
    )

    violations = _scan_operator_access(tmp_path)

    assert violations, (
        "scan returned no violations for a file containing a Funnel-enable pattern. "
        "The Funnel falsifier is broken."
    )
    assert any("Funnel" in desc for _, _, desc in violations), (
        f"Expected a Funnel-enable violation; got: {violations}"
    )
