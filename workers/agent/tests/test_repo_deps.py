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
import io
import subprocess
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from treadmill_agent.repo_deps import (
    RepoOverlay,
    WorkerDepsMaterializationError,
    _detect_archive_kind,
    _install_proxy_url,
    compute_deps_hash,
    materialize,
)
from treadmill_api.models.onboarding import BinarySpec, WorkerDeps


class _FakeResp:
    """Minimal stand-in for urlopen()'s return value."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _build_tar_gz(members: dict[str, bytes]) -> bytes:
    """Pack ``{path: content}`` into a tar.gz byte string."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in members.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _build_zip(members: dict[str, bytes]) -> bytes:
    """Pack ``{path: content}`` into a zip byte string."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for path, content in members.items():
            zf.writestr(path, content)
    return buf.getvalue()


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

    # Redirect the binary write so it lands under tmp_path instead of
    # /var/treadmill/repo-bin (which the test runner cannot create).
    fake_binary_dir = tmp_path / "repo-bin"
    with patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen",
        return_value=_FakeResp(payload),
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


# ── ADR-0077: archive extraction ───────────────────────────────────────────


def test_detect_archive_kind_table() -> None:
    """URL extension dispatches to the right kind; non-archive returns None."""
    assert _detect_archive_kind(
        "https://example/x.tar.gz") == "tar.gz"
    assert _detect_archive_kind(
        "https://example/x.tgz") == "tar.gz"
    assert _detect_archive_kind(
        "https://example/x.tar.bz2") == "tar.bz2"
    assert _detect_archive_kind(
        "https://example/x.tar.xz") == "tar.xz"
    assert _detect_archive_kind(
        "https://example/x.zip") == "zip"
    assert _detect_archive_kind(
        "https://example/cosign") is None
    # Query-string after the extension should not break detection if
    # operators paste a redirect URL — but the current rule is exact
    # suffix match (URL extension is the canonical signal), so a query
    # string trailing the URL falls back to raw. Documented behavior.
    assert _detect_archive_kind(
        "https://example/x.tar.gz?token=abc") is None


def test_materialize_tar_gz_with_top_dir_strips_components(
    tmp_path: Path,
) -> None:
    """tar.gz wrapping content in a single top-level dir → strip-1 hoist.

    Mirrors pulumi's `pulumi-vX.Y.Z-linux-x64.tar.gz` layout:
    ``pulumi/pulumi``, ``pulumi/pulumi-language-nodejs``. Expected
    post-install layout under target_path: ``pulumi`` (the binary) +
    ``pulumi-language-nodejs`` siblings, NOT nested another level
    deep.
    """
    payload = _build_tar_gz({
        "pulumi/pulumi": b"#!/bin/sh\necho main\n",
        "pulumi/pulumi-language-nodejs": b"#!/bin/sh\necho plugin\n",
    })
    digest = hashlib.sha256(payload).hexdigest()
    spec = BinarySpec(
        name="pulumi",
        download_url="https://example.invalid/pulumi-v3.245.0-linux-x64.tar.gz",
        sha256_checksum=digest,
        target_path="/var/treadmill/repo-bin/pulumi",
    )
    fake_binary_dir = tmp_path / "repo-bin"
    with patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen",
        return_value=_FakeResp(payload),
    ), patch(
        "treadmill_agent.repo_deps._BINARY_DIR", fake_binary_dir,
    ):
        materialize(
            "owner/repo",
            WorkerDeps(binaries=[spec]),
            overlay_root=tmp_path,
        )
    target = fake_binary_dir / "pulumi"
    assert (target / "pulumi").is_file(), (
        "main binary should sit directly under target_path after strip-1"
    )
    assert (target / "pulumi-language-nodejs").is_file(), (
        "sibling plugin should sit alongside main binary"
    )
    assert not (target / "pulumi" / "pulumi").exists(), (
        "strip-1 should have collapsed the wrapper dir; nested copy is a bug"
    )
    # All extracted regular files are chmod 0o755.
    assert (target / "pulumi").stat().st_mode & 0o777 == 0o755


def test_materialize_tar_gz_without_top_dir_leaves_as_is(
    tmp_path: Path,
) -> None:
    """Multiple top-level entries → no strip; contents land flat."""
    payload = _build_tar_gz({
        "tool-a": b"#!/bin/sh\necho a\n",
        "tool-b": b"#!/bin/sh\necho b\n",
        "share/README": b"docs\n",
    })
    digest = hashlib.sha256(payload).hexdigest()
    spec = BinarySpec(
        name="multitool",
        download_url="https://example.invalid/multitool.tar.gz",
        sha256_checksum=digest,
        target_path="/var/treadmill/repo-bin/multitool",
    )
    fake_binary_dir = tmp_path / "repo-bin"
    with patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen",
        return_value=_FakeResp(payload),
    ), patch(
        "treadmill_agent.repo_deps._BINARY_DIR", fake_binary_dir,
    ):
        materialize(
            "owner/repo",
            WorkerDeps(binaries=[spec]),
            overlay_root=tmp_path,
        )
    target = fake_binary_dir / "multitool"
    assert (target / "tool-a").is_file()
    assert (target / "tool-b").is_file()
    assert (target / "share" / "README").is_file()


def test_materialize_zip_extracts(tmp_path: Path) -> None:
    """zip payload extracts into target_path; chmod 0o755 applies."""
    payload = _build_zip({
        "kubectl": b"#!/bin/sh\necho kc\n",
    })
    digest = hashlib.sha256(payload).hexdigest()
    spec = BinarySpec(
        name="kubectl",
        download_url="https://example.invalid/kubectl.zip",
        sha256_checksum=digest,
        target_path="/var/treadmill/repo-bin/kubectl",
    )
    fake_binary_dir = tmp_path / "repo-bin"
    with patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen",
        return_value=_FakeResp(payload),
    ), patch(
        "treadmill_agent.repo_deps._BINARY_DIR", fake_binary_dir,
    ):
        materialize(
            "owner/repo",
            WorkerDeps(binaries=[spec]),
            overlay_root=tmp_path,
        )
    extracted = fake_binary_dir / "kubectl" / "kubectl"
    assert extracted.is_file()
    assert extracted.stat().st_mode & 0o777 == 0o755


def test_materialize_raw_binary_regression(tmp_path: Path) -> None:
    """Non-archive URLs continue to land at target_path as a single
    file (ADR-0059 behavior unchanged)."""
    payload = b"\x7fELF stub\n"
    digest = hashlib.sha256(payload).hexdigest()
    spec = BinarySpec(
        name="cosign",
        download_url="https://example.invalid/cosign",
        sha256_checksum=digest,
        target_path="/var/treadmill/repo-bin/cosign",
    )
    fake_binary_dir = tmp_path / "repo-bin"
    with patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen",
        return_value=_FakeResp(payload),
    ), patch(
        "treadmill_agent.repo_deps._BINARY_DIR", fake_binary_dir,
    ):
        materialize(
            "owner/repo",
            WorkerDeps(binaries=[spec]),
            overlay_root=tmp_path,
        )
    target = fake_binary_dir / "cosign"
    assert target.is_file(), (
        "raw binary URL should write target_path as a regular file, "
        "not a directory"
    )
    assert target.read_bytes() == payload
    assert target.stat().st_mode & 0o777 == 0o755


def test_materialize_archive_checksum_mismatch_raises(tmp_path: Path) -> None:
    """sha256 is verified against the raw archive payload (pre-extract);
    a mismatch raises before any extraction work."""
    payload = _build_tar_gz({"x/y": b"hi"})
    wrong_hash = "0" * 64
    spec = BinarySpec(
        name="x",
        download_url="https://example.invalid/x.tar.gz",
        sha256_checksum=wrong_hash,
        target_path="/var/treadmill/repo-bin/x",
    )
    fake_binary_dir = tmp_path / "repo-bin"
    with patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen",
        return_value=_FakeResp(payload),
    ), patch(
        "treadmill_agent.repo_deps._BINARY_DIR", fake_binary_dir,
    ):
        with pytest.raises(WorkerDepsMaterializationError) as exc_info:
            materialize(
                "owner/repo",
                WorkerDeps(binaries=[spec]),
                overlay_root=tmp_path,
            )
    assert exc_info.value.stage == "binary"
    assert "checksum mismatch" in exc_info.value.detail
    assert not (fake_binary_dir / "x").exists(), (
        "no extraction should have occurred when checksum failed"
    )


def test_materialize_archive_path_traversal_refused(tmp_path: Path) -> None:
    """Archive members whose resolved path escapes the extraction root
    raise WorkerDepsMaterializationError (zip-slip guard)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="../etc/passwd")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"evil"))
    payload = buf.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    spec = BinarySpec(
        name="evil",
        download_url="https://example.invalid/evil.tar.gz",
        sha256_checksum=digest,
        target_path="/var/treadmill/repo-bin/evil",
    )
    fake_binary_dir = tmp_path / "repo-bin"
    with patch(
        "treadmill_agent.repo_deps.urllib.request.urlopen",
        return_value=_FakeResp(payload),
    ), patch(
        "treadmill_agent.repo_deps._BINARY_DIR", fake_binary_dir,
    ):
        with pytest.raises(WorkerDepsMaterializationError) as exc_info:
            materialize(
                "owner/repo",
                WorkerDeps(binaries=[spec]),
                overlay_root=tmp_path,
            )
    assert exc_info.value.stage == "binary"
    assert "escapes extraction root" in exc_info.value.detail


def test_env_overrides_includes_bin_path_subdirs(tmp_path: Path) -> None:
    """ADR-0077: archive-extracted tools live in per-spec subdirs under
    bin_path. env_overrides must add each immediate subdir to PATH so
    sibling plugin binaries resolve via lookup."""
    bin_dir = tmp_path / "repo-bin"
    bin_dir.mkdir()
    # Mimic post-install layout: cosign as a raw top-level file +
    # pulumi as an extracted dir containing sibling binaries.
    (bin_dir / "cosign").write_text("#!/bin/sh\n")
    (bin_dir / "pulumi").mkdir()
    (bin_dir / "pulumi" / "pulumi").write_text("#!/bin/sh\n")
    (bin_dir / "pulumi" / "pulumi-language-nodejs").write_text("#!/bin/sh\n")
    (bin_dir / "gcloud").mkdir()
    (bin_dir / "gcloud" / "gcloud").write_text("#!/bin/sh\n")

    overlay = _overlay(bin_path=bin_dir)
    path = overlay.env_overrides()["PATH"].split(":")
    # Top-level bin_dir is on PATH (covers raw cosign).
    assert str(bin_dir) in path
    # Each immediate subdir is on PATH (covers pulumi + gcloud).
    assert str(bin_dir / "pulumi") in path
    assert str(bin_dir / "gcloud") in path
