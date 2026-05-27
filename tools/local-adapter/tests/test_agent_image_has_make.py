"""Agent image must include ``make`` so plan validation scripts that use
``make <target>`` succeed in the worker container.

Structural assertion: read the Dockerfile and confirm ``make`` is listed
in an ``apt-get install`` block (multi-line continuation OK — each package
on its own line is the canonical format).
"""

from __future__ import annotations

import re
from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[2] / "workers" / "agent" / "Dockerfile"


def test_dockerfile_installs_make() -> None:
    """``make`` must appear inside an ``apt-get install`` block in the
    agent Dockerfile. Multi-line continuation (one package per line) is
    the canonical format; the assertion scans the whole RUN block, not
    just a single line."""
    text = _DOCKERFILE.read_text()

    # Find every RUN line that starts an apt-get install block, plus
    # any continuation lines (ending in `\`) following it.
    apt_blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if re.search(r"apt-get\s+install", lines[i]):
            block = [lines[i]]
            j = i
            while j < len(lines) and lines[j].rstrip().endswith("\\"):
                j += 1
                if j < len(lines):
                    block.append(lines[j])
            apt_blocks.append("\n".join(block))
            i = j + 1
        else:
            i += 1

    assert apt_blocks, "no apt-get install blocks found in the agent Dockerfile"
    assert any(
        re.search(r"(^|\s)make(\s|$|\\)", block, re.MULTILINE) for block in apt_blocks
    ), (
        "make is not installed via apt-get in the agent Dockerfile; "
        "plan validation scripts that use `make <target>` will exit 127. "
        f"apt blocks found:\n\n{chr(10).join(apt_blocks)}"
    )
