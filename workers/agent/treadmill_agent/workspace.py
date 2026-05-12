"""Per-step workspace management.

The worker materializes one fresh working tree per step under
``WORKSPACE_DIR``. After the step completes (or fails) the workspace is
removed unless ``KEEP_WORKSPACES=1`` is set (debugging).
"""

from __future__ import annotations

import logging
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("treadmill.agent.workspace")


@contextmanager
def workspace_for_step(workspace_root: str, step_id: str) -> Iterator[Path]:
    """Yield a clean directory for the step. Removes it on exit unless
    ``KEEP_WORKSPACES=1`` is set in the environment."""
    root = Path(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / step_id
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        if os.environ.get("KEEP_WORKSPACES") == "1":
            logger.info("preserving workspace %s (KEEP_WORKSPACES=1)", path)
            return
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            logger.exception("failed to clean workspace %s", path)
