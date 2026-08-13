"""Tests for tools/cc-channels/host_guard.py (ADR-0095 host-binding drift guard).

Unit tests import the module directly (no subprocess). CLI tests run
host_guard.py as __main__ via subprocess to verify exit codes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HOST_GUARD = Path(__file__).resolve().parents[1] / "host_guard.py"

sys.path.insert(0, str(HOST_GUARD.parent))
import host_guard  # noqa: E402


def test_bound_elsewhere_refuses() -> None:
    reason = host_guard.refuse_reason("my-label", "host-a", "host-b")
    assert reason is not None
    assert "host-b" in reason
    assert "host-a" in reason


def test_bound_here_allows() -> None:
    reason = host_guard.refuse_reason("my-label", "rainbow", "rainbow")
    assert reason is None


def test_unbound_refuses_fail_closed() -> None:
    reason = host_guard.refuse_reason("my-label", "rainbow", None)
    assert reason is not None
    assert "fail-closed" in reason.lower() or "no host binding" in reason.lower()


def test_cli_refuses_nonzero() -> None:
    result = subprocess.run(
        [sys.executable, str(HOST_GUARD), "check", "my-label", "other-host"],
        env={**os.environ, "TREADMILL_HOST": "this-host"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}; stderr={result.stderr!r}"
    )
    assert result.stderr.strip() != "", "expected refusal reason on stderr"


def test_cli_allows_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(HOST_GUARD), "check", "my-label", "same-host"],
        env={**os.environ, "TREADMILL_HOST": "same-host"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; stderr={result.stderr!r}"
    )
