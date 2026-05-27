"""Agent image must include ``aws-cdk`` so plan validation scripts that
use ``cdk <command>`` succeed in the worker container.

Structural assertion: read the Dockerfile and confirm ``aws-cdk`` is
listed in an ``npm install`` block (multiple packages on the same line,
or a dedicated install line, are both acceptable).
"""

from __future__ import annotations

import re
from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[3] / "workers" / "agent" / "Dockerfile"


def test_dockerfile_installs_aws_cdk() -> None:
    """``aws-cdk`` must appear inside an ``npm install`` block in the
    agent Dockerfile so plan validation scripts that shell out to
    ``cdk <command>`` resolve the binary."""
    text = _DOCKERFILE.read_text()

    npm_blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if re.search(r"npm\s+install", lines[i]):
            block = [lines[i]]
            j = i
            while j < len(lines) and lines[j].rstrip().endswith("\\"):
                j += 1
                if j < len(lines):
                    block.append(lines[j])
            npm_blocks.append("\n".join(block))
            i = j + 1
        else:
            i += 1

    assert npm_blocks, "no npm install blocks found in the agent Dockerfile"
    assert any(
        re.search(r"(^|\s)aws-cdk(\s|$|\\|@)", block, re.MULTILINE) for block in npm_blocks
    ), (
        "aws-cdk is not installed via npm in the agent Dockerfile; "
        "plan validation scripts that use `cdk <command>` will exit 127. "
        f"npm blocks found:\n\n{chr(10).join(npm_blocks)}"
    )
