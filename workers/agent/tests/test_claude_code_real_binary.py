"""Real Claude Code binary smoke test.

Gated on ``TREADMILL_CLAUDE_BINARY_SMOKE=1`` because the suite otherwise
needs to stay portable to machines without the CLI installed. When the
gate is on, we shell out to ``claude --help`` and assert the flags this
worker depends on still exist — catches an upstream Claude Code release
that renames or removes one of them before the pinned Dockerfile bump
lands.
"""

from __future__ import annotations

import os
import subprocess

import pytest

SMOKE = os.environ.get("TREADMILL_CLAUDE_BINARY_SMOKE") == "1"
pytestmark = pytest.mark.skipif(
    not SMOKE,
    reason="set TREADMILL_CLAUDE_BINARY_SMOKE=1 to run; needs the `claude` CLI installed",
)


def test_claude_help_advertises_flags_we_depend_on() -> None:
    """``claude --help`` must mention the three flags ``claude_code.py``
    shells out with — ``--print``, ``--model``, ``--append-system-prompt``."""
    binary = os.environ.get("CLAUDE_BINARY", "claude")
    result = subprocess.run(
        [binary, "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"`{binary} --help` exited {result.returncode}: {result.stderr}"
    )
    help_text = result.stdout + result.stderr
    for flag in ("--print", "--model", "--append-system-prompt"):
        assert flag in help_text, (
            f"`{binary} --help` does not advertise {flag}; the worker's "
            f"argv layout in claude_code.py needs an update.\n"
            f"--- help text ---\n{help_text}"
        )
