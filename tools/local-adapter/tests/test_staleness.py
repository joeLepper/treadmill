"""Unit tests for the ADR-0069 staleness guard.

Strictly no host I/O outside a tmp path, no real ``os.execv``. Each
test builds a throwaway package on disk, imports it via the standard
sys.path mechanism, and asserts the fingerprint / guard contract
against it. The ``reexec`` path is exercised by monkeypatching
``staleness.os.execv`` so the test verifies the argv passed to it
without actually replacing the test process.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from treadmill_local import staleness
from treadmill_local.staleness import (
    StalenessGuard,
    maybe_reexec,
    package_fingerprint,
)


def _make_pkg(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """Write a tiny package under tmp_path and return its directory.

    ``files`` maps a relative path (e.g. ``"mod.py"``) to file
    contents. The package's ``__init__.py`` is always written even
    when not in ``files`` so the dir is importable as a package.
    """
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    if "__init__.py" not in files:
        (pkg_dir / "__init__.py").write_text("")
    for rel, content in files.items():
        target = pkg_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return pkg_dir


def _import_fresh(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Add tmp_path to sys.path and evict any cached import of ``name``."""
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(name, None)
    # Also evict any cached submodules so a re-import walks fresh disk.
    for mod_name in list(sys.modules):
        if mod_name.startswith(name + "."):
            sys.modules.pop(mod_name, None)


# ── package_fingerprint ───────────────────────────────────────────────────────


def test_package_fingerprint_stable_for_identical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same bytes → same digest across repeated calls."""
    _make_pkg(tmp_path, "pkg_stable", {"mod.py": "x = 1\n"})
    _import_fresh(tmp_path, "pkg_stable", monkeypatch)

    fp1 = package_fingerprint("pkg_stable")
    fp2 = package_fingerprint("pkg_stable")
    assert fp1 == fp2
    assert len(fp1) == 64  # sha256 hexdigest


def test_package_fingerprint_changes_when_tracked_file_bytes_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewriting a tracked file's bytes flips the digest. This is the
    property the StalenessGuard relies on to detect drift."""
    pkg_dir = _make_pkg(tmp_path, "pkg_drift", {"mod.py": "x = 1\n"})
    _import_fresh(tmp_path, "pkg_drift", monkeypatch)

    before = package_fingerprint("pkg_drift")
    (pkg_dir / "mod.py").write_text("x = 999\n")
    after = package_fingerprint("pkg_drift")

    assert before != after


def test_package_fingerprint_changes_when_file_is_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a new .py file in the package counts as drift —
    catches a freshly-introduced submodule between deploys."""
    pkg_dir = _make_pkg(tmp_path, "pkg_added", {"mod.py": "x = 1\n"})
    _import_fresh(tmp_path, "pkg_added", monkeypatch)

    before = package_fingerprint("pkg_added")
    (pkg_dir / "extra.py").write_text("y = 2\n")
    after = package_fingerprint("pkg_added")

    assert before != after


def test_package_fingerprint_ignores_non_py_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fingerprint walks ``*.py`` only — a .txt sibling does not
    perturb the digest. Documents intentional scope: source-code
    change, not arbitrary asset change."""
    pkg_dir = _make_pkg(tmp_path, "pkg_assets", {"mod.py": "x = 1\n"})
    _import_fresh(tmp_path, "pkg_assets", monkeypatch)

    before = package_fingerprint("pkg_assets")
    (pkg_dir / "README.txt").write_text("hello\n")
    after = package_fingerprint("pkg_assets")

    assert before == after


# ── StalenessGuard ────────────────────────────────────────────────────────────


def test_changed_returns_false_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly-constructed guard reports no drift — its startup
    fingerprint matches itself."""
    _make_pkg(tmp_path, "pkg_clean", {"mod.py": "x = 1\n"})
    _import_fresh(tmp_path, "pkg_clean", monkeypatch)

    guard = StalenessGuard("pkg_clean")
    assert guard.changed() is False


def test_changed_returns_true_after_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewriting a tracked .py file after the guard captures its
    startup digest causes ``changed()`` to flip to True."""
    pkg_dir = _make_pkg(tmp_path, "pkg_drift2", {"mod.py": "x = 1\n"})
    _import_fresh(tmp_path, "pkg_drift2", monkeypatch)

    guard = StalenessGuard("pkg_drift2")
    (pkg_dir / "mod.py").write_text("x = 999\n")
    assert guard.changed() is True


def test_changed_swallows_recompute_errors_and_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the recompute raises (e.g. the package vanishes mid-merge),
    the guard treats it as unchanged rather than re-execing into a
    broken state. The guard logs an exception but doesn't propagate."""
    _make_pkg(tmp_path, "pkg_vanish", {"mod.py": "x = 1\n"})
    _import_fresh(tmp_path, "pkg_vanish", monkeypatch)

    guard = StalenessGuard("pkg_vanish")

    def boom(_package: str) -> str:
        raise RuntimeError("transient I/O")

    monkeypatch.setattr(staleness, "package_fingerprint", boom)
    assert guard.changed() is False


def test_guard_module_defaults_to_package_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``module`` is omitted, the guard echoes the package name —
    the simplest case for a single-module entrypoint package."""
    _make_pkg(tmp_path, "pkg_default_mod", {"mod.py": ""})
    _import_fresh(tmp_path, "pkg_default_mod", monkeypatch)

    guard = StalenessGuard("pkg_default_mod")
    assert guard.module == "pkg_default_mod"


def test_guard_module_override_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production processes are spawned via ``python -m
    treadmill_local.autoscaler`` — the guard captures the entrypoint
    module separately from the watched package so the re-exec command
    re-enters the right ``main()``."""
    _make_pkg(tmp_path, "pkg_explicit_mod", {"mod.py": ""})
    _import_fresh(tmp_path, "pkg_explicit_mod", monkeypatch)

    guard = StalenessGuard("pkg_explicit_mod", module="pkg_explicit_mod.entry")
    assert guard.module == "pkg_explicit_mod.entry"


# ── reexec ────────────────────────────────────────────────────────────────────


def test_reexec_calls_execv_with_python_dash_m_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reexec`` must hand ``os.execv`` an argv of the shape
    ``[sys.executable, '-m', <module>, *argv_tail]`` so the re-exec'd
    interpreter re-enters the entrypoint module. Verified without
    actually replacing the process — ``os.execv`` is monkeypatched
    to record its call."""
    _make_pkg(tmp_path, "pkg_reexec", {"mod.py": ""})
    _import_fresh(tmp_path, "pkg_reexec", monkeypatch)

    guard = StalenessGuard("pkg_reexec", module="pkg_reexec.entry")

    calls: list[tuple[str, list[str]]] = []

    def fake_execv(path: str, args: list[str]) -> None:
        calls.append((path, list(args)))
        raise SystemExit("would have exec'd")

    monkeypatch.setattr(staleness.os, "execv", fake_execv)
    monkeypatch.setattr(sys, "argv", ["unused-argv0"])

    with pytest.raises(SystemExit):
        guard.reexec(pid_file=Path("/some/pid/file"))

    assert len(calls) == 1
    path, args = calls[0]
    assert path == sys.executable
    assert args[0] == sys.executable
    assert args[1] == "-m"
    assert args[2] == "pkg_reexec.entry"


def test_reexec_passes_argv_tail_to_new_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extra argv args (anything past argv[0]) propagate to the
    re-exec'd command. This is forward-compat with subprocess
    entrypoints that take CLI flags — the dev-local trio currently
    doesn't, but the contract is here."""
    _make_pkg(tmp_path, "pkg_argv", {"mod.py": ""})
    _import_fresh(tmp_path, "pkg_argv", monkeypatch)

    guard = StalenessGuard("pkg_argv", module="pkg_argv")

    captured: list[list[str]] = []

    def fake_execv(_path: str, args: list[str]) -> None:
        captured.append(list(args))
        raise SystemExit()

    monkeypatch.setattr(staleness.os, "execv", fake_execv)
    monkeypatch.setattr(sys, "argv", ["argv0", "--flag", "value"])

    with pytest.raises(SystemExit):
        guard.reexec()

    assert captured[0][-2:] == ["--flag", "value"]


# ── maybe_reexec convenience ──────────────────────────────────────────────────


class _RecordingGuard:
    """Test double: records changed() / reexec() calls without ever
    calling os.execv. Sufficient for asserting loop-head ordering
    elsewhere in the test suite."""

    def __init__(self, changed: bool) -> None:
        self._changed = changed
        self.changed_calls = 0
        self.reexec_calls: list[Path | None] = []

    def changed(self) -> bool:
        self.changed_calls += 1
        return self._changed

    def reexec(self, pid_file: Path | None = None) -> None:
        self.reexec_calls.append(pid_file)


def test_maybe_reexec_noop_when_guard_is_none() -> None:
    """Disabled-guard path: the function is safe to call and does
    nothing. Used by the legacy fully-local autoscaler that doesn't
    self-heal."""
    # No assertions needed beyond "doesn't raise".
    maybe_reexec(None, Path("/tmp/ignored"))


def test_maybe_reexec_noop_when_not_changed() -> None:
    guard = _RecordingGuard(changed=False)
    maybe_reexec(guard, Path("/tmp/p"))
    assert guard.changed_calls == 1
    assert guard.reexec_calls == []


def test_maybe_reexec_invokes_reexec_when_changed() -> None:
    """Drift case: ``changed()`` returns True, so ``reexec()`` is
    called exactly once with the supplied pid file."""
    guard = _RecordingGuard(changed=True)
    maybe_reexec(guard, Path("/tmp/scheduler.pid"))
    assert guard.changed_calls == 1
    assert guard.reexec_calls == [Path("/tmp/scheduler.pid")]
