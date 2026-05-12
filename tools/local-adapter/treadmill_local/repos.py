"""Local bare-repo provisioning for the agent worker.

When ``REPO_MODE=local`` the worker clones from ``file://`` URLs that
point at bare repos on a host-side directory the runtime mounts into
the container. ``init_bare_repo`` creates one such repo with a single
seed commit on ``main`` so cloning produces a usable working tree.

Layout: ``<bare_repos_dir>/<owner>__<name>.git`` (slash → double
underscore mirrors the worker's ``git.repo_to_directory_name``).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def init_bare_repo(bare_repos_dir: Path, repo: str) -> Path:
    """Create a bare repo for *repo* (``owner/name``) and return its path.

    Idempotent: if the bare repo already exists it is left alone (so a
    user can run ``repo init`` repeatedly without losing prior work).
    """
    bare_repos_dir.mkdir(parents=True, exist_ok=True)
    # Use an absolute path so git's ``remote add origin`` resolves correctly
    # from the seed clone's working directory, not from cwd.
    bare = (bare_repos_dir / f"{repo.replace('/', '__')}.git").resolve()
    if bare.exists():
        return bare

    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )

    # Push a seed commit so subsequent clones see a populated main branch.
    with tempfile.TemporaryDirectory() as tmp:
        seed = Path(tmp)
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(seed)],
            check=True, capture_output=True,
        )
        (seed / "README.md").write_text(
            f"# {repo}\n\nProvisioned by Treadmill local adapter.\n"
        )
        for cmd in (
            ["git", "-C", str(seed), "config", "user.email", "agent@treadmill.local"],
            ["git", "-C", str(seed), "config", "user.name", "Treadmill Agent"],
            ["git", "-C", str(seed), "add", "-A"],
            ["git", "-C", str(seed), "commit", "-m", "init: treadmill seed"],
            ["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
            ["git", "-C", str(seed), "push", "origin", "main"],
        ):
            subprocess.run(cmd, check=True, capture_output=True)
    return bare
