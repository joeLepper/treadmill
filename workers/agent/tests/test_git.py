"""Git ops tests using a real local bare repo.

These exercise the ``local`` mode end-to-end against a real
``git init --bare`` fixture and the ``github`` mode against argv-recording
Python stubs on PATH. Live ``gh auth`` is not exercised here — Phase 4
E.3 covers the real-GitHub smoke against a personal AWS account.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from treadmill_agent import git


@pytest.fixture
def bare_repos_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bare"
    d.mkdir()
    return d


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


def _init_bare(bare_repos_dir: Path, repo: str) -> Path:
    bare = bare_repos_dir / f"{git.repo_to_directory_name(repo)}.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)], check=True)
    # Seed the bare repo with one commit so clone has something to base on.
    seed = bare_repos_dir.parent / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", str(seed)], check=True)
    (seed / "README.md").write_text("# repo\n")
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True)
    shutil.rmtree(seed)
    return bare


def test_repo_to_directory_name_replaces_slash() -> None:
    assert git.repo_to_directory_name("owner/name") == "owner__name"


def test_clone_local_mode_returns_repo_dir(
    bare_repos_dir: Path, workspace_dir: Path,
) -> None:
    _init_bare(bare_repos_dir, "owner/test-repo")
    repo_dir = git.clone(
        repo="owner/test-repo", mode="local",
        bare_repos_dir=str(bare_repos_dir), workspace=workspace_dir,
    )
    assert repo_dir == workspace_dir / "repo"
    assert (repo_dir / ".git").is_dir()
    assert (repo_dir / "README.md").exists()


def test_clone_local_mode_raises_when_bare_missing(
    bare_repos_dir: Path, workspace_dir: Path,
) -> None:
    with pytest.raises(git.GitOpsError, match="does not exist"):
        git.clone(
            repo="missing/repo", mode="local",
            bare_repos_dir=str(bare_repos_dir), workspace=workspace_dir,
        )


@pytest.mark.parametrize("mode", ["zonk", "wat", ""])
def test_clone_unknown_mode_raises(
    bare_repos_dir: Path, workspace_dir: Path, mode: str,
) -> None:
    """``local`` and ``github`` are the supported modes; any other value
    is a config error and must fail loudly."""
    with pytest.raises(git.GitOpsError, match="unknown REPO_MODE"):
        git.clone(
            repo="owner/test-repo", mode=mode,
            bare_repos_dir=str(bare_repos_dir), workspace=workspace_dir,
        )


@pytest.mark.parametrize("mode", ["zonk", "wat", ""])
def test_open_pr_unknown_mode_raises(tmp_path: Path, mode: str) -> None:
    """``open_pr`` mirrors ``clone``: only ``local`` and ``github`` are valid."""
    with pytest.raises(git.GitOpsError, match="unknown REPO_MODE"):
        git.open_pr(
            repo_dir=tmp_path, branch="b", title="t", body="b",
            repo="owner/test-repo", mode=mode,
        )


def test_full_local_cycle_clone_branch_commit_push_pr(
    bare_repos_dir: Path, workspace_dir: Path,
) -> None:
    """End-to-end against a real bare repo: changes pushed via the worker
    helpers are visible by re-cloning the bare repo from a different dir."""
    bare = _init_bare(bare_repos_dir, "owner/test-repo")
    repo_dir = git.clone(
        repo="owner/test-repo", mode="local",
        bare_repos_dir=str(bare_repos_dir), workspace=workspace_dir,
    )
    git.checkout_branch(repo_dir, "task/abc123-add-thing")
    (repo_dir / "src.py").write_text("print('hi')\n")
    sha = git.commit_all(repo_dir, "feat: add src\n\nTreadmill-Task-Id: t-1\n")
    git.push_branch(repo_dir, "task/abc123-add-thing")
    pr_number, pr_url = git.open_pr(
        repo_dir=repo_dir, branch="task/abc123-add-thing",
        title="t", body="b", repo="owner/test-repo", mode="local",
    )

    # local mode does not synthesize a remote PR.
    assert pr_number is None
    assert pr_url is None
    assert len(sha) == 40

    # Verify the bare repo received the branch.
    out = subprocess.run(
        ["git", "-C", str(bare), "branch", "--list", "task/abc123-add-thing"],
        capture_output=True, text=True, check=True,
    )
    assert "task/abc123-add-thing" in out.stdout


# ── B.2 staged-changes helpers ────────────────────────────────────────────────


def test_commit_all_raises_when_nothing_staged(
    bare_repos_dir: Path, workspace_dir: Path,
) -> None:
    """``commit_all`` no longer takes ``--allow-empty``. A commit with
    nothing staged is a real failure that surfaces as ``GitOpsError``
    so the runner can publish ``step.failed`` instead of silently
    pushing an empty commit (the B.2 invariant)."""
    _init_bare(bare_repos_dir, "owner/test-repo")
    repo_dir = git.clone(
        repo="owner/test-repo", mode="local",
        bare_repos_dir=str(bare_repos_dir), workspace=workspace_dir,
    )
    git.checkout_branch(repo_dir, "task/empty")
    # No files added — staging is empty.
    with pytest.raises(git.GitOpsError):
        git.commit_all(repo_dir, "should fail")


def test_has_staged_changes_true_when_added(
    bare_repos_dir: Path, workspace_dir: Path,
) -> None:
    _init_bare(bare_repos_dir, "owner/test-repo")
    repo_dir = git.clone(
        repo="owner/test-repo", mode="local",
        bare_repos_dir=str(bare_repos_dir), workspace=workspace_dir,
    )
    git.checkout_branch(repo_dir, "task/has-changes")
    (repo_dir / "src.py").write_text("print('hi')\n")
    git.stage_all(repo_dir)
    assert git.has_staged_changes(repo_dir) is True


def test_has_staged_changes_false_on_clean_tree(
    bare_repos_dir: Path, workspace_dir: Path,
) -> None:
    _init_bare(bare_repos_dir, "owner/test-repo")
    repo_dir = git.clone(
        repo="owner/test-repo", mode="local",
        bare_repos_dir=str(bare_repos_dir), workspace=workspace_dir,
    )
    git.checkout_branch(repo_dir, "task/clean")
    # No edits, no staging.
    assert git.has_staged_changes(repo_dir) is False


# ── B.3 redelivery safety ─────────────────────────────────────────────────────


def test_checkout_branch_idempotent_when_branch_exists_on_origin(
    bare_repos_dir: Path, workspace_dir: Path,
) -> None:
    """SQS may redeliver the same claim after the worker pushed its
    branch. A re-run must:

      1. Recognize the existing ``origin/<branch>`` and start from there
         (``git checkout -B`` survives "already exists"; the fetch +
         remote-ref probe picks the right base).
      2. Push the (possibly identical) commits back via
         ``--force-with-lease`` without rejection.

    Concretely: seed the bare repo with ``task/redelivered`` carrying
    an unrelated commit, then run the full clone + checkout + commit +
    push cycle and assert the push succeeds.
    """
    bare = _init_bare(bare_repos_dir, "owner/test-repo")
    # Seed the bare with a pre-existing target branch (an arbitrary
    # commit different from ``main``'s tip).
    seed = bare_repos_dir.parent / "preseed"
    seed.mkdir()
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "checkout", "-b", "task/redelivered"],
        check=True,
    )
    (seed / "prior.txt").write_text("prior tip\n")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "prior"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "task/redelivered"],
        check=True,
    )
    shutil.rmtree(seed)

    # Now the worker re-attempts the step: clone a fresh tree,
    # checkout the (already-existing) branch, commit, and push.
    repo_dir = git.clone(
        repo="owner/test-repo", mode="local",
        bare_repos_dir=str(bare_repos_dir), workspace=workspace_dir,
    )
    git.checkout_branch(repo_dir, "task/redelivered")
    # ``prior.txt`` should be in the working tree because we branched
    # from origin/task/redelivered, not origin/main.
    assert (repo_dir / "prior.txt").exists()
    (repo_dir / "added.txt").write_text("new\n")
    git.commit_all(repo_dir, "redelivery commit")
    # ``--force-with-lease`` succeeds because our local tip is a
    # descendant of the lease target (the prior tip we just fetched).
    git.push_branch(repo_dir, "task/redelivered")

    # Verify the new commit landed.
    out = subprocess.run(
        ["git", "-C", str(bare), "log", "--oneline", "task/redelivered"],
        capture_output=True, text=True, check=True,
    )
    assert "redelivery commit" in out.stdout


def test_push_with_force_with_lease_rejects_concurrent_unknown_writes(
    bare_repos_dir: Path, workspace_dir: Path, tmp_path: Path,
) -> None:
    """A concurrent unknown writer must not be silently clobbered.

    Sequence:
      1. Worker clones, branches, commits, pushes ``task/raced``.
      2. A separate writer pushes a different commit onto the same
         branch directly on the bare repo — the worker doesn't know
         about it.
      3. Worker attempts to push *again* on top of its stale local
         view. ``--force-with-lease`` must reject because the lease
         target (what the worker last knew) is no longer the bare's
         tip.
    """
    bare = _init_bare(bare_repos_dir, "owner/test-repo")
    repo_dir = git.clone(
        repo="owner/test-repo", mode="local",
        bare_repos_dir=str(bare_repos_dir), workspace=workspace_dir,
    )
    git.checkout_branch(repo_dir, "task/raced")
    (repo_dir / "first.txt").write_text("first\n")
    git.commit_all(repo_dir, "first")
    git.push_branch(repo_dir, "task/raced")

    # Simulate a concurrent writer landing a different commit on the
    # same branch directly on the bare repo.
    racer = tmp_path / "racer"
    racer.mkdir()
    subprocess.run(["git", "clone", str(bare), str(racer)], check=True)
    subprocess.run(["git", "-C", str(racer), "config", "user.email", "r@r"], check=True)
    subprocess.run(["git", "-C", str(racer), "config", "user.name", "r"], check=True)
    subprocess.run(
        ["git", "-C", str(racer), "checkout", "task/raced"], check=True,
    )
    (racer / "racer.txt").write_text("racer\n")
    subprocess.run(["git", "-C", str(racer), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(racer), "commit", "-m", "racer"], check=True)
    subprocess.run(
        ["git", "-C", str(racer), "push", "origin", "task/raced"],
        check=True,
    )

    # The worker, unaware of the racer's push, makes another local
    # commit and tries to push. The lease target is its previously-known
    # tip — which no longer matches origin — so the push must fail.
    (repo_dir / "second.txt").write_text("second\n")
    git.commit_all(repo_dir, "second")
    with pytest.raises(git.GitOpsError):
        git.push_branch(repo_dir, "task/raced")


# ── D.1 github mode ──────────────────────────────────────────────────────────
#
# We stub ``git`` and ``gh`` as small bash binaries on PATH (the same
# pattern ``test_claude_code.py`` uses for the ``claude`` binary). The
# stubs record their argv + relevant env vars to JSON files so each test
# can assert what was invoked.
#
# Leak-prevention regression: every test seeds a sentinel PAT value into
# the environment via a marker var and asserts that sentinel never
# appears in any recorded argv or env. The real ``gh`` keyring path is
# not exercised here (would require live network); the contract under
# test is "the worker code never embeds the PAT in argv / env / URL."


_PAT_SENTINEL = "ghp_THIS_IS_A_TEST_PAT_SENTINEL_DO_NOT_LEAK_xyz123"


def _read_stub_log(log_path: Path) -> list[dict]:
    import json
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


_PY_STUB = '''#!/usr/bin/env python3
"""Test stub for git / gh. Records argv + env into a JSONL log."""
import json
import os
import sys

LOG_PATH = "__LOG_PATH__"
BIN_NAME = "__BIN_NAME__"

with open("/proc/self/cmdline", "rb") as f:
    proc_cmdline = f.read().replace(b"\\x00", b" ").decode(errors="replace").strip()

record = {
    "bin": BIN_NAME,
    "argv": sys.argv[1:],
    "env": {
        k: v for k, v in os.environ.items()
        if k in {"GH_TOKEN", "GITHUB_TOKEN", "GH_HOST",
                 "GITHUB_PAT", "TREADMILL_TEST_MARKER"}
    },
    "proc_cmdline": proc_cmdline,
}
with open(LOG_PATH, "a") as f:
    f.write(json.dumps(record) + "\\n")

# Behavioral stubs so the worker code can proceed.
if BIN_NAME == "git" and len(sys.argv) >= 4 and sys.argv[1] == "clone":
    # sys.argv[2] is url, sys.argv[3] is target path.
    target = sys.argv[3]
    os.makedirs(os.path.join(target, ".git"), exist_ok=True)
elif BIN_NAME == "gh" and len(sys.argv) >= 3 and sys.argv[1] == "pr" and sys.argv[2] == "create":
    sys.stdout.write("https://github.com/owner/test-repo/pull/42\\n")

sys.exit(0)
'''


def _install_stub(bin_dir: Path, name: str, log_path: Path) -> Path:
    target = bin_dir / name
    target.write_text(
        _PY_STUB.replace("__LOG_PATH__", str(log_path)).replace(
            "__BIN_NAME__", name,
        )
    )
    target.chmod(0o755)
    return target


@pytest.fixture
def github_mode_stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Stand up a fake ``git`` + ``gh`` on PATH and return per-binary logs.

    The stubs are Python scripts that record argv + a curated env subset
    + the kernel's ``/proc/<pid>/cmdline`` view into JSONL files. The
    ``TREADMILL_TEST_MARKER`` env var is exported so each test can
    sanity-check that the recording mechanism is alive (the marker
    must appear) while the PAT sentinel must not.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_log = tmp_path / "git_calls.jsonl"
    gh_log = tmp_path / "gh_calls.jsonl"
    _install_stub(bin_dir, "git", git_log)
    _install_stub(bin_dir, "gh", gh_log)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("TREADMILL_TEST_MARKER", "marker-present")
    return {"git_log": git_log, "gh_log": gh_log, "bin_dir": bin_dir}


def _assert_no_pat_leak(stub_calls: list[dict]) -> None:
    """Every recorded stub call must be PAT-free in argv and /proc cmdline.

    The PAT sentinel must NOT appear in argv (or the kernel's view of
    the cmdline). Env-var inheritance can carry ``GITHUB_PAT`` to the
    child if the parent had it set — that's the operator's problem to
    avoid, not the worker code's contract. The contract is "our code
    never puts the PAT into argv or into a fresh env var on the
    subprocess." That's tested via argv + the absence of ``GH_TOKEN``
    / ``GITHUB_TOKEN`` (the env vars ``gh`` itself would consult).
    """
    for call in stub_calls:
        for arg in call["argv"]:
            assert _PAT_SENTINEL not in arg, (
                f"PAT sentinel leaked into argv of {call['bin']}: {call}"
            )
        assert _PAT_SENTINEL not in call["proc_cmdline"], (
            f"PAT sentinel leaked into /proc/<pid>/cmdline of {call['bin']}: {call}"
        )


def test_clone_github_mode_has_no_token_in_url(
    workspace_dir: Path, github_mode_stubs: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """github-mode clone must invoke ``git clone https://github.com/...``
    with no ``x-access-token:<PAT>@`` in the URL — auth lives in ``gh``'s
    credential helper, populated at worker startup."""
    # Seed the PAT into the test process's env so we can prove our worker
    # code doesn't propagate it down. ``gh auth login --with-token``
    # uses stdin only; nothing should pipe this var into a child argv.
    monkeypatch.setenv("GITHUB_PAT", _PAT_SENTINEL)

    repo_dir = git.clone(
        repo="owner/test-repo", mode="github",
        bare_repos_dir="/unused/in/github/mode",
        workspace=workspace_dir,
    )
    assert repo_dir == workspace_dir / "repo"

    git_calls = _read_stub_log(github_mode_stubs["git_log"])
    # Find the clone call (there will also be `git -C <dir> config` calls).
    clone_calls = [c for c in git_calls if "clone" in c["argv"]]
    assert len(clone_calls) == 1, f"expected one clone call, got {git_calls}"
    clone_argv = clone_calls[0]["argv"]
    # URL is the second-to-last element (clone <url> <target>).
    url = clone_argv[clone_argv.index("clone") + 1]
    assert url == "https://github.com/owner/test-repo.git"
    assert "x-access-token" not in url
    assert "@" not in url  # no embedded credential prefix
    _assert_no_pat_leak(git_calls)


def test_open_pr_github_mode_shells_to_gh_and_returns_url(
    tmp_path: Path, github_mode_stubs: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """github-mode ``open_pr`` must shell to ``gh pr create`` (not embed
    a token anywhere) and parse the returned URL into ``(pr_number, pr_url)``.
    """
    monkeypatch.setenv("GITHUB_PAT", _PAT_SENTINEL)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    pr_number, pr_url = git.open_pr(
        repo_dir=repo_dir, branch="task/abc",
        title="t", body="b", repo="owner/test-repo", mode="github",
    )
    assert pr_url == "https://github.com/owner/test-repo/pull/42"
    assert pr_number == 42

    gh_calls = _read_stub_log(github_mode_stubs["gh_log"])
    # Two calls per task #120's idempotency: ``gh pr list`` to check for an
    # existing PR for this branch (returns empty in this test's stub), then
    # ``gh pr create`` to open a new one.
    assert len(gh_calls) == 2
    list_argv = gh_calls[0]["argv"]
    assert list_argv[:3] == ["pr", "list", "--head"]
    create_argv = gh_calls[1]["argv"]
    assert create_argv[:3] == ["pr", "create", "--title"]
    # Our worker code does not set GH_TOKEN — even though the test process
    # has GITHUB_PAT set, the recorded gh env must NOT carry it as GH_TOKEN
    # (which is what ``gh`` would use for auth). The keyring is the only
    # channel.
    for call in gh_calls:
        assert "GH_TOKEN" not in call["env"]
        assert "GITHUB_TOKEN" not in call["env"]
    _assert_no_pat_leak(gh_calls)


def test_github_mode_round_trip_never_leaks_pat_sentinel(
    workspace_dir: Path, github_mode_stubs: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end leak regression: clone + open_pr in github mode.

    Even with ``GITHUB_PAT=<sentinel>`` set on the worker process's env
    (the worst case: an operator accidentally exported the var before
    starting the worker), our code path must not propagate the sentinel
    into any ``git`` or ``gh`` invocation's argv. The PAT may flow from
    the worker process to a child only via the stdin of
    ``gh auth login --with-token`` — and that call happens in
    ``startup_auth.py``, not here.
    """
    monkeypatch.setenv("GITHUB_PAT", _PAT_SENTINEL)

    repo_dir = git.clone(
        repo="owner/test-repo", mode="github",
        bare_repos_dir="/unused", workspace=workspace_dir,
    )
    git.open_pr(
        repo_dir=repo_dir, branch="b", title="t", body="b",
        repo="owner/test-repo", mode="github",
    )

    git_calls = _read_stub_log(github_mode_stubs["git_log"])
    gh_calls = _read_stub_log(github_mode_stubs["gh_log"])
    all_calls = git_calls + gh_calls
    # The marker should appear (proves env recording works).
    assert any(
        c["env"].get("TREADMILL_TEST_MARKER") == "marker-present" for c in all_calls
    ), "marker env var should have been recorded — recording mechanism broken"
    # The PAT sentinel must not appear in any argv or in any of the env
    # vars our stubs record (``GH_TOKEN`` / ``GITHUB_TOKEN`` / etc).
    # ``GITHUB_PAT`` would inherit through env into child processes —
    # we record it too and assert specifically that our worker code
    # does not pass it through as ``GH_TOKEN`` (which is what ``gh``
    # would consult). The keyring populated at startup is the only
    # auth channel.
    for call in all_calls:
        for token_var in ("GH_TOKEN", "GITHUB_TOKEN"):
            assert token_var not in call["env"], (
                f"worker code passed {token_var} to {call['bin']}: {call}"
            )
        for arg in call["argv"]:
            assert _PAT_SENTINEL not in arg, (
                f"PAT sentinel leaked into argv of {call['bin']}: {call}"
            )
