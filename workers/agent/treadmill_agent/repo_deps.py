"""Per-repo extras materialization + overlay env (ADR-0059 step 2).

ADR-0059 lets onboarders register a small Python / Node package set
plus a curated binary list per repo. The worker materializes those
extras into an overlay directory before task work runs (and reuses the
overlay across steps via a ``.deps-hash`` cache file). Validation
subprocesses then see the overlay via env vars (``PATH`` /
``PYTHONPATH`` / ``NODE_PATH``) so ``cdk synth`` / ``ruff`` / a
repo-specific CLI resolves to the materialized copy instead of failing
with ``ModuleNotFoundError`` or ``command not found``.

Sibling of :mod:`treadmill_agent.startup_auth`'s
``fetch_claude_credentials`` plumbing: the runner calls
:func:`materialize` after the App re-mint, binds the result via
:func:`bind_overlay` for the duration of ``_execute``, and resets it in
a ``finally`` so the next step starts unbound. Subprocess sites that
spawn user-supplied scripts read the bound overlay via
:func:`current_overlay` and merge ``env_overrides()`` into the child
env.

The materialization side-effects (venv create, ``pip install``,
``npm install --prefix``, binary download + sha256 verify) all live
behind ``subprocess.run`` / ``urllib.request.urlopen`` so the worker
takes on no new dependency.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import subprocess
import urllib.request
from contextvars import Token
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

if TYPE_CHECKING:
    from treadmill_api.models.onboarding import BinarySpec, WorkerDeps

logger = logging.getLogger("treadmill.agent.repo_deps")

_DEFAULT_OVERLAY_ROOT = Path("/var/treadmill/repo-overlays")
_BINARY_DIR = Path("/var/treadmill/repo-bin")
_SUBPROCESS_TIMEOUT = 300


class WorkerDepsMaterializationError(RuntimeError):
    """Raised when materialization fails at a specific stage.

    ``stage`` is one of ``'python'`` / ``'node'`` / ``'binary'`` so the
    runner / step failure event can surface which install phase blew
    up. ``detail`` carries the captured stderr (or checksum mismatch
    message) for the operator.
    """

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


@dataclass(frozen=True)
class RepoOverlay:
    """Resolved overlay paths for a repo's materialized extras.

    ``fresh=True`` means this :func:`materialize` call ran the installs
    (cache miss); ``fresh=False`` means the prior ``.deps-hash`` already
    matched so the overlay was reused, or that the repo's
    ``WorkerDeps`` was empty and there's nothing to overlay.
    """

    repo: str
    deps_hash: str
    venv_path: Path | None
    node_modules_path: Path | None
    bin_path: Path | None
    fresh: bool

    def env_overrides(self) -> dict[str, str]:
        """Env vars to merge onto a subprocess so it picks up the overlay.

        Returns an empty dict when the overlay has no materialized
        paths (empty ``WorkerDeps``) — the caller can unconditionally
        merge without an ``if overlay`` guard.

        ``PATH`` is built fresh from the overlay (caller is expected to
        merge with the parent env's ``PATH``); the per-key
        implementation lives in the subprocess seam, which already has
        access to ``os.environ``.
        """
        if (
            self.venv_path is None
            and self.node_modules_path is None
            and self.bin_path is None
        ):
            return {}

        path_parts: list[str] = []
        if self.venv_path is not None:
            path_parts.append(str(self.venv_path / "bin"))
        if self.bin_path is not None:
            path_parts.append(str(self.bin_path))
        if self.node_modules_path is not None:
            path_parts.append(str(self.node_modules_path / ".bin"))

        existing_path = os.environ.get("PATH", "")
        if existing_path:
            path_parts.append(existing_path)

        env: dict[str, str] = {"PATH": ":".join(path_parts)}
        if self.venv_path is not None:
            site_packages = _venv_site_packages(self.venv_path)
            if site_packages is not None:
                env["PYTHONPATH"] = str(site_packages)
        if self.node_modules_path is not None:
            env["NODE_PATH"] = str(self.node_modules_path)
        return env


_CURRENT_OVERLAY: contextvars.ContextVar[RepoOverlay | None] = (
    contextvars.ContextVar("_repo_overlay", default=None)
)


def bind_overlay(overlay: RepoOverlay) -> Token:
    """Bind ``overlay`` for the calling context; pair with :func:`reset_overlay`."""
    return _CURRENT_OVERLAY.set(overlay)


def reset_overlay(token: Token) -> None:
    """Undo a prior :func:`bind_overlay`; pair them in ``try`` / ``finally``."""
    _CURRENT_OVERLAY.reset(token)


def current_overlay() -> RepoOverlay | None:
    """Return the overlay bound for the calling context, or ``None``."""
    return _CURRENT_OVERLAY.get()


def compute_deps_hash(worker_deps: "WorkerDeps") -> str:
    """Canonical sha256 over a ``WorkerDeps``.

    Lists are sorted before serialization so reordering input elements
    produces the same digest — the cache key is the *set* of pinned
    specs, not the order they were entered in onboarding.
    """
    python_sorted = sorted(worker_deps.python)
    node_sorted = sorted(worker_deps.node)
    binaries_sorted = sorted(
        (
            {
                "name": b.name,
                "download_url": b.download_url,
                "sha256_checksum": b.sha256_checksum,
                "target_path": b.target_path,
            }
            for b in worker_deps.binaries
        ),
        key=lambda d: (d["name"], d["target_path"]),
    )
    payload = {
        "python": python_sorted,
        "node": node_sorted,
        "binaries": binaries_sorted,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def materialize(
    repo: str,
    worker_deps: "WorkerDeps",
    *,
    overlay_root: Path = _DEFAULT_OVERLAY_ROOT,
) -> RepoOverlay:
    """Materialize ``worker_deps`` for ``repo`` and return the overlay.

    Empty ``WorkerDeps`` short-circuits to an overlay with all paths
    ``None`` and ``fresh=False`` — the subprocess seam sees an empty
    ``env_overrides()`` and merges nothing.

    Cache key is :func:`compute_deps_hash`. If the overlay dir's
    ``.deps-hash`` file already matches, the previously-materialized
    paths are reused with ``fresh=False``.

    On cache miss, the overlay dir is (re)built: python first (venv +
    pip install), then node (``npm install --prefix``), then binaries
    (urllib download + sha256 verify). Each phase wraps its
    :class:`subprocess.CalledProcessError` /
    :class:`urllib.error.URLError` / checksum-mismatch in
    :class:`WorkerDepsMaterializationError` tagged with the failing
    stage. The ``.deps-hash`` file is written last so a partial failure
    forces a full re-run next time rather than caching a half-built
    overlay.
    """
    deps_hash = compute_deps_hash(worker_deps)
    has_python = bool(worker_deps.python)
    has_node = bool(worker_deps.node)
    has_binaries = bool(worker_deps.binaries)

    if not (has_python or has_node or has_binaries):
        return RepoOverlay(
            repo=repo,
            deps_hash=deps_hash,
            venv_path=None,
            node_modules_path=None,
            bin_path=None,
            fresh=False,
        )

    overlay_dir = overlay_root / _slugify_repo(repo)
    venv_path = overlay_dir / "venv" if has_python else None
    node_modules_path = overlay_dir / "node_modules" if has_node else None
    bin_path = _BINARY_DIR if has_binaries else None

    hash_file = overlay_dir / ".deps-hash"
    if hash_file.is_file() and hash_file.read_text().strip() == deps_hash:
        logger.info(
            "repo_deps cache hit: repo=%s hash=%s overlay=%s",
            repo, deps_hash, overlay_dir,
        )
        return RepoOverlay(
            repo=repo,
            deps_hash=deps_hash,
            venv_path=venv_path,
            node_modules_path=node_modules_path,
            bin_path=bin_path,
            fresh=False,
        )

    logger.info(
        "repo_deps cache miss: repo=%s hash=%s overlay=%s",
        repo, deps_hash, overlay_dir,
    )
    overlay_dir.mkdir(parents=True, exist_ok=True)

    if has_python:
        _install_python(venv_path, worker_deps.python)
    if has_node:
        _install_node(overlay_dir, worker_deps.node)
    if has_binaries:
        _install_binaries(worker_deps.binaries)

    hash_file.write_text(deps_hash)
    return RepoOverlay(
        repo=repo,
        deps_hash=deps_hash,
        venv_path=venv_path,
        node_modules_path=node_modules_path,
        bin_path=bin_path,
        fresh=True,
    )


def _install_proxy_url() -> str | None:
    """Credentialed proxy URL for install-phase egress (ADR-0060).

    Combines ``TREADMILL_INSTALL_PROXY_TOKEN`` (minted per worker by the
    autoscaler) with the worker's base ``HTTPS_PROXY`` to form
    ``http://install:<token>@<host>:<port>`` — the only signal the
    egress proxy honors when deciding whether to elevate a request to
    the install-phase allowlist. Returns ``None`` when either env var
    is missing so the caller falls back to the task-phase
    (uncredentialed) proxy that the worker entrypoint already set.

    Stripping any existing userinfo from the base URL keeps the helper
    idempotent if a caller has already credentialed the proxy.
    """
    token = os.environ.get("TREADMILL_INSTALL_PROXY_TOKEN")
    base = os.environ.get("HTTPS_PROXY")
    if not token or not base:
        return None
    parsed = urlparse(base)
    netloc_host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunparse(parsed._replace(netloc=f"install:{token}@{netloc_host}"))


def _install_subprocess_env() -> dict[str, str] | None:
    """Subprocess env for materialize()'s install-phase children.

    Returns a copy of ``os.environ`` with ``HTTPS_PROXY`` / ``HTTP_PROXY``
    overridden to the credentialed proxy URL when one is available, so
    ``pip`` / ``npm`` route through the install-phase allowlist.
    Returns ``None`` when no credential is configured, which the
    callers pass straight to ``subprocess.run(env=...)`` — equivalent
    to inheriting the parent env (the task-phase contract: uncredentialed
    ``HTTPS_PROXY`` if present, otherwise no proxy at all).
    """
    proxy_url = _install_proxy_url()
    if proxy_url is None:
        return None
    return {**os.environ, "HTTPS_PROXY": proxy_url, "HTTP_PROXY": proxy_url}


def _slugify_repo(repo: str) -> str:
    """``owner/name`` → ``owner__name`` so the overlay dir is path-safe."""
    return repo.replace("/", "__")


def _venv_site_packages(venv_path: Path) -> Path | None:
    """Locate ``<venv>/lib/python<X.Y>/site-packages`` for ``PYTHONPATH``.

    Returns ``None`` when the venv hasn't been created yet (the unit
    tests mock subprocess so the directory never exists). Callers that
    do find an existing venv get the canonical site-packages dir; in
    practice every cpython venv on Linux has exactly one match.
    """
    if not venv_path.is_dir():
        return None
    matches = sorted(venv_path.glob("lib/python*/site-packages"))
    if not matches:
        return None
    return matches[0]


def _install_python(venv_path: Path | None, specs: list[str]) -> None:
    assert venv_path is not None
    env = _install_subprocess_env()
    try:
        subprocess.run(
            ["python", "-m", "venv", str(venv_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise WorkerDepsMaterializationError(
            stage="python",
            detail=f"venv create failed: {exc.stderr or exc.stdout or exc}",
        ) from exc
    try:
        subprocess.run(
            [str(venv_path / "bin" / "pip"), "install", *specs],
            check=True,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise WorkerDepsMaterializationError(
            stage="python",
            detail=f"pip install failed: {exc.stderr or exc.stdout or exc}",
        ) from exc


def _install_node(overlay_dir: Path, specs: list[str]) -> None:
    try:
        subprocess.run(
            ["npm", "install", "--prefix", str(overlay_dir), *specs],
            check=True,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            env=_install_subprocess_env(),
        )
    except subprocess.CalledProcessError as exc:
        raise WorkerDepsMaterializationError(
            stage="node",
            detail=f"npm install failed: {exc.stderr or exc.stdout or exc}",
        ) from exc


def _install_binaries(binaries: list["BinarySpec"]) -> None:
    # Re-anchor each spec's target_path under the (module-level)
    # ``_BINARY_DIR`` so the binary lands where the rest of the worker
    # expects it. The Pydantic validator on ``BinarySpec.target_path``
    # already pins the prefix to ``/var/treadmill/repo-bin/``; we strip
    # that prefix and re-join against ``_BINARY_DIR`` so unit tests can
    # redirect installs by patching the module attribute.
    from treadmill_api.models.onboarding import BINARY_TARGET_PREFIX

    _BINARY_DIR.mkdir(parents=True, exist_ok=True)
    proxy_url = _install_proxy_url()
    for spec in binaries:
        relative = spec.target_path[len(BINARY_TARGET_PREFIX):]
        target = _BINARY_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if proxy_url is not None:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler(
                        {"http": proxy_url, "https": proxy_url},
                    ),
                )
                with opener.open(  # noqa: S310 — onboarding-curated URL
                    spec.download_url, timeout=_SUBPROCESS_TIMEOUT,
                ) as resp:
                    payload = resp.read()
            else:
                with urllib.request.urlopen(  # noqa: S310 — onboarding-curated URL
                    spec.download_url, timeout=_SUBPROCESS_TIMEOUT,
                ) as resp:
                    payload = resp.read()
        except Exception as exc:  # noqa: BLE001
            raise WorkerDepsMaterializationError(
                stage="binary",
                detail=f"download failed for {spec.name}: {exc}",
            ) from exc
        actual = hashlib.sha256(payload).hexdigest()
        if actual != spec.sha256_checksum:
            raise WorkerDepsMaterializationError(
                stage="binary",
                detail=(
                    f"checksum mismatch for {spec.name}: "
                    f"expected={spec.sha256_checksum} actual={actual}"
                ),
            )
        target.write_bytes(payload)
        target.chmod(0o755)
