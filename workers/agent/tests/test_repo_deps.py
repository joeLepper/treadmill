"""Tests for repo_deps module (ADR-0059 step 2).

Covers the deps-hash determinism contract, the materialize cache-miss
/ cache-hit / empty-deps branches, per-stage failure mapping into
:class:`WorkerDepsMaterializationError`, and the env_overrides shape
produced by :class:`RepoOverlay`.

All subprocess + urllib calls are mocked — the suite never installs
real packages or downloads real binaries.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from treadmill_agent.repo_deps import (
    RepoOverlay,
    WorkerDepsMaterializationError,
    _install_proxy_url,
    compute_deps_hash,
    materialize,
)
from treadmill_api.models.onboarding import BinarySpec, WorkerDeps


# ── compute_deps_hash ──────────────────────────────────────────────────────


def test_compute_deps_hash_deterministic() -> None:
    """Identical inputs (and identical inputs in different order) hash equal."""
    a = WorkerDeps(
        python=["aws-cdk-lib==2.214.0", "boto3==1.35.0"],
        node=["typescript@5.4.0"],
        binaries=[],
    )
    b = WorkerDeps(
        python=["boto3==1.35.0", "aws-cdk-lib==2.214.0"],  # reordered
        node=["typescript@5.4.0"],
        binaries=[],
    )
    assert compute_deps_hash(a) == compute_deps_hash(b)


def test_compute_deps_hash_differs_on_content() -> None:
    """Changing a single pinned spec changes the hash."""
    a = WorkerDeps(python=["aws-cdk-lib==2.214.0"])
    b = WorkerDeps(python=["aws-cdk-lib==2.215.0"])
    assert compute_deps_hash(a) != compute_deps_hash(b)


# ── materialize: short-circuit on empty ────────────────────────────────────


def test_materialize_empty_worker_deps_short_circuits(tmp_path: Path) -> None:
    """Empty WorkerDeps → all paths None, fresh=False, no subprocess calls."""
    with patch(
        "treadmill_agent.repo_deps.subprocess.run"
    ) as mock_run, patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen"
    ) as mock_urlopen:
        overlay = materialize(
            "owner/repo", WorkerDeps(), overlay_root=tmp_path,
        )
    assert overlay.fresh is False
    assert overlay.venv_path is None
    assert overlay.node_modules_path is None
    assert overlay.bin_path is None
    mock_run.assert_not_called()
    mock_urlopen.assert_not_called()


# ── materialize: python install ────────────────────────────────────────────


def test_materialize_python_deps_calls_pip(tmp_path: Path) -> None:
    """python=[...] triggers ``python -m venv`` then ``pip install``."""
    worker_deps = WorkerDeps(python=["aws-cdk-lib==2.214.0"])

    with patch("treadmill_agent.repo_deps.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        overlay = materialize(
            "owner/repo", worker_deps, overlay_root=tmp_path,
        )

    assert overlay.fresh is True
    assert overlay.venv_path is not None
    assert overlay.node_modules_path is None
    assert overlay.bin_path is None
    # Two calls: venv create, pip install
    assert mock_run.call_count == 2
    venv_call = mock_run.call_args_list[0]
    assert venv_call.args[0][:3] == ["python", "-m", "venv"]
    pip_call = mock_run.call_args_list[1]
    assert pip_call.args[0][-1] == "aws-cdk-lib==2.214.0"
    assert "pip" in pip_call.args[0][0]


# ── materialize: cache hit ─────────────────────────────────────────────────


def test_materialize_cache_hit_short_circuits(tmp_path: Path) -> None:
    """Existing .deps-hash matching computed hash → no subprocess calls,
    fresh=False, overlay paths still populated for the validation seam."""
    worker_deps = WorkerDeps(python=["aws-cdk-lib==2.214.0"])
    expected_hash = compute_deps_hash(worker_deps)
    overlay_dir = tmp_path / "owner__repo"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / ".deps-hash").write_text(expected_hash)

    with patch(
        "treadmill_agent.repo_deps.subprocess.run"
    ) as mock_run, patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen"
    ) as mock_urlopen:
        overlay = materialize(
            "owner/repo", worker_deps, overlay_root=tmp_path,
        )
    assert overlay.fresh is False
    assert overlay.venv_path == overlay_dir / "venv"
    mock_run.assert_not_called()
    mock_urlopen.assert_not_called()


# ── materialize: python failure ────────────────────────────────────────────


def test_materialize_python_install_failure_raises(tmp_path: Path) -> None:
    """subprocess CalledProcessError during pip install → stage='python'."""
    worker_deps = WorkerDeps(python=["bogus-pkg==9.9.9"])

    def _fake_run(*args, **kwargs):
        # First call (venv) succeeds; second (pip install) fails.
        cmd = args[0]
        if cmd[:3] == ["python", "-m", "venv"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr="",
            )
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd,
            output="", stderr="ERROR: No matching distribution",
        )

    with patch(
        "treadmill_agent.repo_deps.subprocess.run", side_effect=_fake_run,
    ):
        with pytest.raises(WorkerDepsMaterializationError) as exc_info:
            materialize("owner/repo", worker_deps, overlay_root=tmp_path)
    assert exc_info.value.stage == "python"
    assert "No matching distribution" in exc_info.value.detail


# ── materialize: binary checksum mismatch ──────────────────────────────────


def test_materialize_binary_checksum_mismatch_raises(tmp_path: Path) -> None:
    """urlopen returns known bytes; declared sha256 differs → stage='binary'."""
    payload = b"hello world"
    wrong_hash = "0" * 64  # never matches payload
    spec = BinarySpec(
        name="hello",
        download_url="https://example.invalid/hello",
        sha256_checksum=wrong_hash,
        target_path="/var/treadmill/repo-bin/hello",
    )
    worker_deps = WorkerDeps(binaries=[spec])

    class _FakeResp:
        def read(self) -> bytes:
            return payload
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    # Redirect the binary write so it lands under tmp_path instead of
    # /var/treadmill/repo-bin (which the test runner cannot create).
    fake_binary_dir = tmp_path / "repo-bin"
    with patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen",
        return_value=_FakeResp(),
    ), patch(
        "treadmill_agent.repo_deps._BINARY_DIR", fake_binary_dir,
    ):
        with pytest.raises(WorkerDepsMaterializationError) as exc_info:
            materialize("owner/repo", worker_deps, overlay_root=tmp_path)
    assert exc_info.value.stage == "binary"
    assert "checksum mismatch" in exc_info.value.detail
    actual = hashlib.sha256(payload).hexdigest()
    assert actual in exc_info.value.detail


# ── env_overrides shape ────────────────────────────────────────────────────


def _overlay(
    *,
    venv_path: Path | None = None,
    node_modules_path: Path | None = None,
    bin_path: Path | None = None,
) -> RepoOverlay:
    return RepoOverlay(
        repo="owner/repo",
        deps_hash="x" * 64,
        venv_path=venv_path,
        node_modules_path=node_modules_path,
        bin_path=bin_path,
        fresh=False,
    )


def test_env_overrides_empty_when_no_overlay() -> None:
    """Overlay with all paths None → empty dict (no env merge needed)."""
    overlay = _overlay()
    assert overlay.env_overrides() == {}


def test_env_overrides_shape_with_python_only(tmp_path: Path) -> None:
    """Python-only overlay sets PATH (prepending venv/bin) + PYTHONPATH."""
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    overlay = _overlay(venv_path=venv)
    env = overlay.env_overrides()
    assert "PATH" in env
    assert env["PATH"].startswith(str(venv / "bin"))
    assert env["PYTHONPATH"] == str(site_packages)
    assert "NODE_PATH" not in env


def test_env_overrides_shape_with_all_three(tmp_path: Path) -> None:
    """All three paths → PATH prepends venv/bin + bin_path + node_modules/.bin;
    PYTHONPATH + NODE_PATH set."""
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    bin_dir = tmp_path / "repo-bin"
    bin_dir.mkdir()

    overlay = _overlay(
        venv_path=venv, node_modules_path=node_modules, bin_path=bin_dir,
    )
    env = overlay.env_overrides()
    path = env["PATH"]
    # Order is venv/bin → bin_path → node_modules/.bin → existing PATH.
    venv_idx = path.index(str(venv / "bin"))
    bin_idx = path.index(str(bin_dir))
    node_idx = path.index(str(node_modules / ".bin"))
    assert venv_idx < bin_idx < node_idx
    assert env["PYTHONPATH"] == str(site_packages)
    assert env["NODE_PATH"] == str(node_modules)


# ── _install_proxy_url + install-phase subprocess env (ADR-0060 step 3c) ────


def test_install_proxy_url_returns_none_when_token_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No install credential in env → no credentialed URL; caller falls
    back to the task-phase (uncredentialed) proxy."""
    monkeypatch.delenv("TREADMILL_INSTALL_PROXY_TOKEN", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://treadmill-egress-proxy:3128")
    assert _install_proxy_url() is None


def test_install_proxy_url_returns_none_when_https_proxy_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No base HTTPS_PROXY → no credentialed URL even with the token —
    the helper has no host:port to anchor the credential against."""
    monkeypatch.setenv("TREADMILL_INSTALL_PROXY_TOKEN", "abc123")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    assert _install_proxy_url() is None


def test_install_proxy_url_returns_credentialed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token + base proxy → ``http://install:<token>@<host>:<port>``."""
    monkeypatch.setenv("TREADMILL_INSTALL_PROXY_TOKEN", "abc123")
    monkeypatch.setenv("HTTPS_PROXY", "http://treadmill-egress-proxy:3128")
    assert (
        _install_proxy_url()
        == "http://install:abc123@treadmill-egress-proxy:3128"
    )


def test_materialize_subprocess_env_has_credentialed_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TREADMILL_INSTALL_PROXY_TOKEN is set, every materialize()
    subprocess.run call receives an env override with HTTPS_PROXY /
    HTTP_PROXY pointing at the credentialed proxy URL. The egress
    proxy reads the Proxy-Authorization header off this URL to grant
    install-phase allowlist access."""
    monkeypatch.setenv("TREADMILL_INSTALL_PROXY_TOKEN", "abc123")
    monkeypatch.setenv("HTTPS_PROXY", "http://treadmill-egress-proxy:3128")
    worker_deps = WorkerDeps(python=["aws-cdk-lib==2.214.0"])

    with patch("treadmill_agent.repo_deps.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        materialize("owner/repo", worker_deps, overlay_root=tmp_path)

    expected = "http://install:abc123@treadmill-egress-proxy:3128"
    assert mock_run.call_count >= 1
    for call in mock_run.call_args_list:
        env = call.kwargs.get("env")
        assert env is not None, (
            f"materialize() subprocess.run missing env kwarg: {call}"
        )
        assert env.get("HTTPS_PROXY") == expected
        assert env.get("HTTP_PROXY") == expected


def test_materialize_subprocess_env_unchanged_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TREADMILL_INSTALL_PROXY_TOKEN is unset, materialize() does
    not override the subprocess env — env kwarg is None (inherit parent
    env). The worker entrypoint's uncredentialed HTTPS_PROXY (if any)
    flows through unchanged; nothing escalates to install-phase."""
    monkeypatch.delenv("TREADMILL_INSTALL_PROXY_TOKEN", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://treadmill-egress-proxy:3128")
    worker_deps = WorkerDeps(python=["aws-cdk-lib==2.214.0"])

    with patch("treadmill_agent.repo_deps.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        materialize("owner/repo", worker_deps, overlay_root=tmp_path)

    assert mock_run.call_count >= 1
    for call in mock_run.call_args_list:
        # env=None is the contract for "unchanged" — subprocess.run
        # inherits the parent env (and any HTTPS_PROXY in it stays
        # uncredentialed).
        assert call.kwargs.get("env") is None
