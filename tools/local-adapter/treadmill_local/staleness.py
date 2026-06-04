"""Staleness guard — managed host processes self-heal on source change (ADR-0069).

Long-running host processes spawned by ``treadmill-local up`` — the
autoscaler, scheduler, and deploy-watcher — can drift out of sync
with the working tree once an operator merges a PR. Without this
guard the deploy-watcher's ``adapter`` category was notify-only: a
``tools/local-adapter/**`` merge silently left the autoscaler and
the watcher itself running pre-merge bytes until the operator
restarted ``treadmill-local up`` by hand.

Strategy: each process records a content fingerprint of its package's
``*.py`` bytes at startup; at the TOP of every loop iteration — a
safe re-exec point, no tick in flight, no half-recreated container —
it recomputes and compares. If the bytes drifted, ``os.execv``
replaces the process with a fresh interpreter and the new ``main()``
rewrites its own PID file so the parent's pid-file machinery stays
consistent.

The deploy-watcher additionally re-execs ITSELF first, then restarts
its siblings (autoscaler + scheduler) via the injected
``restart_host_processes_fn`` accelerator — turning a slow self-heal
(each sibling noticing on its own next tick) into a fast one as soon
as the merge is observed.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import sys
from pathlib import Path
from typing import NoReturn

logger = logging.getLogger("treadmill.staleness")

DEFAULT_PACKAGE = "treadmill_local"


def package_fingerprint(package: str = DEFAULT_PACKAGE) -> str:
    """sha256 over the sorted ``*.py`` bytes of an installed package.

    Reads bytes, not mtime, so a ``touch`` on an unchanged file is
    not a change. The walk is sorted-by-path and the relative path is
    folded into the digest, so a rename registers as a change too.
    Subpackages are included (``rglob`` recurses).

    The package directory is resolved from the imported module's
    ``__file__``, which is the canonical way to get the on-disk
    location for both editable installs and site-packages installs.
    """
    mod = importlib.import_module(package)
    pkg_file = getattr(mod, "__file__", None)
    if not pkg_file:
        raise RuntimeError(f"package {package!r} has no __file__")
    pkg_dir = Path(pkg_file).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(pkg_dir.rglob("*.py")):
        rel = str(path.relative_to(pkg_dir))
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class StalenessGuard:
    """Holds a startup fingerprint; reports whether source has changed.

    Construct ONCE before the loop. ``changed()`` is cheap (one walk,
    one sha256) and is safe to call every iteration. ``reexec()`` calls
    ``os.execv``, which does not return — it replaces the current
    process image with a fresh interpreter.

    ``module`` is the dotted name passed back through ``python -m`` on
    re-exec; defaults to the package itself (suitable when the
    package is a single-module entrypoint, otherwise the caller passes
    its entrypoint — e.g. ``"treadmill_local.autoscaler"``).
    """

    def __init__(
        self,
        package: str = DEFAULT_PACKAGE,
        *,
        module: str | None = None,
    ) -> None:
        self._package = package
        self._module = module or package
        self._startup_fingerprint = package_fingerprint(package)

    @property
    def package(self) -> str:
        return self._package

    @property
    def module(self) -> str:
        return self._module

    @property
    def startup_fingerprint(self) -> str:
        return self._startup_fingerprint

    def changed(self) -> bool:
        try:
            current = package_fingerprint(self._package)
        except Exception:
            # Partial sync (mid-merge, transient I/O) shouldn't push us
            # to re-exec into a broken state — wait for the next tick.
            logger.exception(
                "staleness: fingerprint recompute failed; assuming unchanged"
            )
            return False
        return current != self._startup_fingerprint

    def reexec(self, pid_file: Path | None = None) -> NoReturn:
        """Replace this process with a fresh ``python -m <module>``.

        Logs the re-exec loudly, flushes log handlers so the line
        lands on disk before exec wipes the file table, then calls
        ``os.execv``. The re-exec'd ``main()`` is responsible for
        rewriting ``pid_file`` with its own pid — the post-exec pid is
        unknown to this caller, so writing it here would be wrong.
        ``pid_file`` is accepted for diagnostic logging only and to
        keep call sites symmetric across the three host processes.
        """
        argv_tail = sys.argv[1:]
        cmd = [sys.executable, "-m", self._module, *argv_tail]
        logger.warning(
            "re-execing: source changed (package=%s, module=%s, pid_file=%s)",
            self._package,
            self._module,
            pid_file,
        )
        for handler in list(logger.handlers) + list(logging.getLogger().handlers):
            try:
                handler.flush()
            except Exception:
                pass
        os.execv(sys.executable, cmd)


def maybe_reexec(
    guard: StalenessGuard | None,
    pid_file: Path | None = None,
) -> None:
    """Loop-head convenience: re-exec if the guard reports drift.

    Pass ``None`` for ``guard`` to disable the check — used by tests
    and by the legacy fully-local autoscaler that has no self-heal
    semantics. Call at the TOP of the loop, before the iteration's
    work begins.
    """
    if guard is None:
        return
    if guard.changed():
        guard.reexec(pid_file)
