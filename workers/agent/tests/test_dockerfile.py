"""Dockerfile policy tests.

We parse the Dockerfile (not build it) to enforce policies a code-review
might miss — pinning the Claude Code CLI version is the only mechanical
check we have for "don't ship an unpinned global install of a tool the
agent's behavior depends on."
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


def test_claude_code_install_pins_version() -> None:
    """The ``npm install -g @anthropic-ai/claude-code`` line must include
    an ``@<semver>`` suffix so unattended image rebuilds get the same
    binary the suite was written against."""
    content = DOCKERFILE.read_text()
    pattern = re.compile(
        r"npm\s+install\s+-g\s+@anthropic-ai/claude-code(@\d+\.\d+\.\d+(?:\.\d+)?)"
    )
    match = pattern.search(content)
    assert match is not None, (
        "expected `npm install -g @anthropic-ai/claude-code@<semver>` in Dockerfile; "
        f"got content snippet:\n{content}"
    )
