"""Git operations for the worker.

Two ``REPO_MODE`` values are supported:

  * ``local`` — clone from a file:// bare repo at
    ``<bare_repos_dir>/<owner>__<name>.git``. Pushes go back to the
    same bare repo. "PR creation" is a no-op that synthesizes no
    remote-PR identifier; the events table records the branch via
    ``output.branch`` and leaves ``pr_number`` / ``pr_url`` null.

  * ``github`` — clone over HTTPS from ``github.com``. Auth lives in
    ``gh``'s keyring (populated at worker startup by
    ``__main__.py`` via ``gh auth login --with-token`` + ``gh auth
    setup-git``); the PAT is **never** in the URL, **never** in env,
    **never** on disk outside ``gh``'s own config. PR creation shells
    to ``gh pr create``. Re-introduced in Phase 4 (Week 4 D.1, after
    being removed in Week 2 B.7 for shipping without coverage).

Anything other than ``local`` / ``github`` raises ``GitOpsError``.

Redelivery safety (B.3)
-----------------------

SQS at-least-once redelivery means the worker can re-enter
``_execute`` for a step whose previous attempt already produced a
local branch — either in the worker's freshly-cloned working tree
or on ``origin``. Two design choices guard against that:

  * ``checkout_branch`` uses ``git checkout -B`` (capital B) so the
    local branch is created or *reset* to the chosen base — it never
    fails with "branch already exists". The base is ``origin/<branch>``
    when that ref exists, otherwise ``origin/main``. ``git fetch
    origin`` runs first so the remote refs are accurate.
  * ``push_branch`` uses ``git push --force-with-lease``. The same
    worker re-pushing the same commits succeeds (the lease target
    matches), but a concurrent unknown writer trips the lease check
    and the push is rejected.

No-empty-commits (B.2)
----------------------

``commit_all`` no longer takes ``--allow-empty``; a commit with
nothing staged is a real failure that should bubble up to the
runner and become a ``step.failed`` event. The runner calls
``stage_all`` + ``has_staged_changes`` before ``commit_all`` to
distinguish "Claude Code produced no changes" from a deeper git
fault.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("treadmill.agent.git")


@dataclass(frozen=True)
class GitOpsResult:
    branch: str
    commit_sha: str
    pr_number: int | None
    pr_url: str | None


class GitOpsError(RuntimeError):
    """Surface git failures with stderr captured for the events table."""


def repo_to_directory_name(repo: str) -> str:
    """Map ``owner/name`` → ``owner__name`` for filesystem-friendly paths.

    Slash is allowed on Linux but doubled-underscore is the convention we
    use across the local-adapter so the bare-repo provisioning code and
    worker agree on layout.
    """
    return repo.replace("/", "__")


def clone(
    *,
    repo: str,
    mode: str,
    bare_repos_dir: str,
    workspace: Path,
) -> Path:
    """Clone the repo into ``<workspace>/repo`` and return that path."""
    target = workspace / "repo"
    if mode == "local":
        bare = Path(bare_repos_dir) / f"{repo_to_directory_name(repo)}.git"
        if not bare.exists():
            raise GitOpsError(
                f"local bare repo {bare} does not exist; "
                "provision it via the local adapter or initialize manually"
            )
        url = f"file://{bare}"
        _run(["git", "clone", url, str(target)])
        _configure_local_identity(target)
        return target
    if mode == "github":
        # No token in the URL. ``gh auth setup-git`` (invoked at worker
        # startup in ``__main__.py``) installed the credential helper so
        # plain ``git clone https://github.com/...`` reaches ``gh`` for
        # auth without the PAT ever appearing in argv, env, or
        # ``.git/config``. Per ADR-0016 Q16.d.
        url = f"https://github.com/{repo}.git"
        _run(["git", "clone", url, str(target)])
        _configure_local_identity(target)
        return target
    raise GitOpsError(f"unknown REPO_MODE: {mode!r}")


def checkout_branch(repo_dir: Path, branch: str) -> None:
    """Create (or reset) the local ``branch`` so the worker can write to it.

    Redelivery safety: SQS may redeliver the same claim after the worker
    already produced this branch upstream. We fetch ``origin`` so the
    remote refs are accurate, then run ``git checkout -B`` (capital B) so
    the local branch is created or hard-reset — it never fails with
    "branch already exists". When ``origin/<branch>`` exists we use it
    as the base so the local tip matches what's already on the remote;
    otherwise we branch from ``origin/main``.
    """
    _run(["git", "-C", str(repo_dir), "fetch", "origin"])
    # Resolve which remote ref to branch from. ``git rev-parse --verify``
    # exits 0 when the ref exists, non-zero otherwise — we don't want
    # ``_run`` to raise on the non-zero case, so use subprocess directly.
    logger.info("$ git -C %s rev-parse --verify origin/%s", repo_dir, branch)
    probe = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify",
         f"refs/remotes/origin/{branch}"],
        capture_output=True, text=True,
    )
    base = f"origin/{branch}" if probe.returncode == 0 else "origin/main"
    _run(["git", "-C", str(repo_dir), "checkout", "-B", branch, base])


def stage_all(repo_dir: Path) -> None:
    """``git add -A`` — stage every change in the working tree."""
    _run(["git", "-C", str(repo_dir), "add", "-A"])


def has_staged_changes(repo_dir: Path) -> bool:
    """Return True iff there is at least one staged change.

    Uses ``git diff --cached --quiet`` which exits 0 when nothing is
    staged and 1 when staged changes exist. Any other exit code is a
    real git failure and surfaces as a ``GitOpsError``.
    """
    logger.info("$ git -C %s diff --cached --quiet", repo_dir)
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise GitOpsError(
        f"git diff --cached failed: exit {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def commit_all(repo_dir: Path, message: str) -> str:
    """Stage everything and commit; return the new commit SHA.

    Empty commits are rejected (no ``--allow-empty``). Callers that need
    to distinguish "no changes" from a deeper git fault should call
    ``stage_all`` + ``has_staged_changes`` first.
    """
    stage_all(repo_dir)
    _run(["git", "-C", str(repo_dir), "commit", "-m", message])
    return _capture(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).strip()


def push_branch(repo_dir: Path, branch: str) -> None:
    """Push ``branch`` to ``origin`` with ``--force-with-lease``.

    Redelivery safety: the same worker re-pushing identical commits
    succeeds (the lease target matches the remote tip we just fetched).
    A concurrent unknown writer that landed a commit we didn't fetch
    trips the lease check and the push is rejected — better to fail
    loudly than silently clobber another writer's work.
    """
    _run([
        "git", "-C", str(repo_dir), "push", "--force-with-lease",
        "-u", "origin", branch,
    ])


def open_pr(
    *,
    repo_dir: Path,
    branch: str,
    title: str,
    body: str,
    repo: str,
    mode: str,
) -> tuple[int | None, str | None]:
    """Open a PR, idempotent for re-author workflows.

    ``local`` mode is a no-op (no remote service to host a PR); the
    branch itself is the artifact.

    ``github`` mode is idempotent: if a PR for ``branch`` already exists,
    returns its number/URL. Otherwise shells to ``gh pr create``. Auth comes
    from the ``gh`` keyring populated at worker startup — we do **not** set
    ``GH_TOKEN`` in env (which would route the PAT through every
    child process's environment). The command runs with ``cwd=repo_dir``
    so ``gh`` discovers the upstream from the cloned repo's
    ``origin``.

    When the branch no longer exists on the remote (merged+deleted case),
    logs a warning and returns (None, None) rather than raising.
    """
    if mode == "local":
        # The branch is the artifact in local mode. The events table records
        # the branch via ``output.branch``; pr_number/url stay null.
        return None, None
    if mode == "github":
        # Check if a PR already exists for this branch.
        existing = _get_existing_pr(repo_dir, branch)
        if existing is not None:
            pr_number, pr_url = existing
            logger.info(
                "PR %s already exists for branch %s; returning existing PR",
                pr_number, branch
            )
            return pr_number, pr_url

        # Try to create a new PR. If the branch doesn't exist on the remote
        # (merged+deleted case), log and skip rather than raising.
        try:
            stdout = _capture(
                [
                    "gh", "pr", "create",
                    "--title", title,
                    "--body", body,
                    "--head", branch,
                ],
                cwd=repo_dir,
            )
            pr_url = _last_url(stdout)
            pr_number = _pr_number_from_url(pr_url) if pr_url else None
            return pr_number, pr_url
        except GitOpsError as e:
            # If the branch doesn't exist or the create fails, log and skip.
            logger.warning(
                "Failed to create PR for branch %s (may be merged+deleted): %s",
                branch, str(e)
            )
            return None, None
    raise GitOpsError(f"unknown REPO_MODE: {mode!r}")


# ── helpers ──────────────────────────────────────────────────────────────────


def _configure_local_identity(repo_dir: Path) -> None:
    """Set a Treadmill identity so commits don't error on missing
    user.email / user.name in fresh containers."""
    _run([
        "git", "-C", str(repo_dir), "config", "user.email",
        os.environ.get("GIT_AUTHOR_EMAIL", "agent@treadmill.local"),
    ])
    _run([
        "git", "-C", str(repo_dir), "config", "user.name",
        os.environ.get("GIT_AUTHOR_NAME", "Treadmill Agent"),
    ])


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    logger.info("$ %s", " ".join(cmd))
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise GitOpsError(
            f"command failed: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    if result.stdout:
        logger.debug(result.stdout)


def _get_existing_pr(repo_dir: Path, branch: str) -> tuple[int, str] | None:
    """Check if a PR already exists for the given branch.

    Uses ``gh pr list --head <branch>`` to check for existing PRs.
    Returns (pr_number, pr_url) if found, None otherwise.
    """
    try:
        stdout = _capture(
            ["gh", "pr", "list", "--head", branch, "--json", "number,url"],
            cwd=repo_dir,
        )
        # The output is JSON array. Empty array means no PRs exist.
        prs = json.loads(stdout)
        if not prs:
            return None
        # Return the first (should be only one) PR.
        pr = prs[0]
        pr_number = pr.get("number")
        pr_url = pr.get("url")
        if pr_number and pr_url:
            return int(pr_number), pr_url
    except (GitOpsError, json.JSONDecodeError, KeyError):
        # If the command fails or JSON is malformed, assume no PR exists.
        pass
    return None


def _last_url(stdout: str) -> str | None:
    """Return the last whitespace-separated token that looks like a URL.

    ``gh pr create`` prints the PR URL on its final line; some versions
    interleave a "Creating pull request for ..." preamble. Scanning
    bottom-up for the first ``https://`` token tolerates both shapes.
    """
    for line in reversed(stdout.splitlines()):
        for token in line.split():
            if token.startswith("https://"):
                return token.rstrip(".,")
    return None


def _pr_number_from_url(url: str) -> int | None:
    """Extract the PR number from a ``gh pr create`` URL.

    URLs look like ``https://github.com/<owner>/<repo>/pull/<number>``.
    Returns ``None`` rather than raising if the URL doesn't match the
    expected shape — the caller still has the URL itself, so a slightly
    weird ``gh`` output should not break the runner.
    """
    parts = url.rstrip("/").split("/")
    if len(parts) < 2 or parts[-2] != "pull":
        return None
    try:
        return int(parts[-1])
    except ValueError:
        return None


def head_sha(repo_dir: Path) -> str:
    """Return the full SHA of HEAD in ``repo_dir``.

    Used by the review and validate dispositions to attach the
    canonical commit_sha to their StepOutput envelopes — the
    mergeability VIEW joins reviews + validations on this SHA, so
    a missing value silently breaks auto-merge eligibility.
    """
    return _capture(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
    ).strip()


def get_head_diff_text(
    repo_dir: Path, *, max_chars: int,
) -> tuple[str, bool]:
    """Return the diff of HEAD vs its parent, capped at ``max_chars``.

    Returns ``(diff_text, truncated)`` where ``truncated`` is True iff the
    original diff exceeded ``max_chars`` and the returned text was shortened.

    Uses ``git show --no-color HEAD`` so the output excludes terminal color
    codes (would inflate size + confuse the architect's prompt). The format
    string is empty to suppress the commit-message preamble; the architect
    only needs the unified diff body.

    Used by the code disposition to capture the rejected diff when
    author-side validation fails (the repo_dir is torn down at step end,
    so this is the only surviving copy of the worker's attempted change).
    See ADR-0048 follow-on (PR for ``capture-rejected-diff-for-architect``).
    """
    text = _capture(
        [
            "git", "-C", str(repo_dir), "show",
            "--no-color", "--format=", "HEAD",
        ],
    )
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return text, truncated


def _capture(cmd: list[str], *, cwd: Path | None = None) -> str:
    logger.info("$ %s", " ".join(cmd))
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise GitOpsError(
            f"command failed: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout
