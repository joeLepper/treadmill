"""LocalRuntime helper tests.

Focused unit coverage for behavior on `LocalRuntime` that doesn't need
a live Docker daemon. The constructor calls `docker.from_env()`, so we
exercise the method-under-test via the unbound function with a fake
``self`` object (or by constructing the instance with the docker client
stubbed at import time).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from treadmill_local import runtime
from treadmill_local.runner import ContainerSpec
from treadmill_local.runtime import AGENT_FAMILY, LocalRuntime


def _make_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalRuntime:
    """Build a LocalRuntime with the docker client stubbed.

    The constructor calls ``docker.from_env()`` for the side-effect of
    binding ``self.docker``; we don't need a real daemon for this test.
    """
    class _FakeDocker:
        pass

    monkeypatch.setattr(runtime.docker, "from_env", lambda: _FakeDocker())
    return LocalRuntime(tmp_path)


def _agent_spec() -> ContainerSpec:
    return ContainerSpec(
        family=AGENT_FAMILY,
        name="agent",
        image="treadmill-agent:local",
    )


def test_volumes_for_agent_family_mounts_credentials_rw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Claude credentials bind must be ``rw`` so Claude Code can
    refresh the host user's OAuth token in place — ``ro`` causes silent
    auth failures once the token expires."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    creds = fake_home / ".claude" / ".credentials.json"
    creds.write_text("{}")
    monkeypatch.setattr(runtime.Path, "home", classmethod(lambda cls: fake_home))

    rt = _make_runtime(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)  # bare-repos dir is relative to cwd
    mounts = rt._volumes_for(_agent_spec())

    assert str(creds) in mounts
    assert mounts[str(creds)] == {
        "bind": "/root/.claude/.credentials.json",
        "mode": "rw",
    }


def test_volumes_for_postgres_mounts_named_volume_for_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres family gets a deployment-scoped named volume so DB state
    survives ``down`` + ``up``. Fully-local (no deployment_config) uses
    ``treadmill-local-postgres-data``. Redis / API containers still
    have no mounts."""
    rt = _make_runtime(tmp_path, monkeypatch)

    pg = ContainerSpec(family="treadmill-postgres", name="postgres", image="postgres:16")
    pg_mounts = rt._volumes_for(pg)
    assert pg_mounts == {
        "treadmill-local-postgres-data": {
            "bind": "/var/lib/postgresql/data",
            "mode": "rw",
        }
    }

    api = ContainerSpec(family="treadmill-api", name="api", image="treadmill-api:dev")
    assert rt._volumes_for(api) == {}


def test_volumes_for_agent_family_skips_credentials_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the host user has no Claude credentials yet, the bare-repos
    volume is still wired up so local-mode clones work."""
    fake_home = tmp_path / "home-noclaude"
    fake_home.mkdir()
    monkeypatch.setattr(runtime.Path, "home", classmethod(lambda cls: fake_home))

    rt = _make_runtime(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    mounts = rt._volumes_for(_agent_spec())

    # No credentials key — but the bare-repos mount is still there.
    creds_path = fake_home / ".claude" / ".credentials.json"
    assert str(creds_path) not in mounts
    assert any(
        spec["bind"] == "/var/treadmill/repos"
        for spec in mounts.values()
    )


# ── parse_github_origin (dev-local deploy-watcher spawn env) ──────────────────


def _init_repo_with_origin(repo_root: Path, origin_url: str) -> None:
    """Create a bare-bones git repo with a single ``origin`` remote."""
    import subprocess

    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", origin_url],
        cwd=repo_root,
        check=True,
    )


@pytest.mark.parametrize(
    "origin_url, expected",
    [
        ("https://github.com/joeLepper/treadmill.git", ("joeLepper", "treadmill")),
        ("https://github.com/joeLepper/treadmill", ("joeLepper", "treadmill")),
        ("git@github.com:joeLepper/treadmill.git", ("joeLepper", "treadmill")),
        ("git@github.com:joeLepper/treadmill", ("joeLepper", "treadmill")),
        ("http://github.com/acme/repo-with-dashes.git", ("acme", "repo-with-dashes")),
    ],
)
def test_parse_github_origin_extracts_owner_and_repo(
    tmp_path: Path, origin_url: str, expected: tuple[str, str]
) -> None:
    """The deploy-watcher spawn derives ``GITHUB_OWNER``/``GITHUB_REPO`` from
    the local checkout's ``origin`` remote, so the operator doesn't have to
    export them by hand. Cover both URL styles GitHub serves."""
    _init_repo_with_origin(tmp_path, origin_url)
    assert runtime.parse_github_origin(tmp_path) == expected


def test_parse_github_origin_rejects_non_github_remote(tmp_path: Path) -> None:
    """A non-GitHub remote isn't usable by the watcher (which calls the
    GitHub REST API). Fail loudly at spawn time, not at the watcher's first
    HTTP call."""
    _init_repo_with_origin(tmp_path, "https://gitlab.com/group/proj.git")
    with pytest.raises(RuntimeError, match="does not look like a GitHub URL"):
        runtime.parse_github_origin(tmp_path)


def test_parse_github_origin_errors_without_origin_remote(tmp_path: Path) -> None:
    """No ``origin`` remote — say so clearly rather than letting the watcher
    crash with a less-informative ``KeyError`` after start-up."""
    import subprocess

    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    with pytest.raises(RuntimeError, match="could not read 'origin' remote"):
        runtime.parse_github_origin(tmp_path)
