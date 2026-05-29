"""Wiring integration tests for ADR-0059 step 6.

This file complements :mod:`tests.test_repo_deps` (which covers
:func:`treadmill_agent.repo_deps.materialize` in isolation) by crossing
the ``repo_deps`` → ``validation_runtime`` module boundary in a single
test. The two existing unit-test suites both patch
``subprocess.run`` at *their own* module — ``repo_deps`` for the
install seam, ``validation_runtime`` for the script seam — so neither
exercises the ContextVar handoff that connects them.

A future regression that drops or skips the ``current_overlay()``
lookup inside :func:`treadmill_agent.validation_runtime.run_deterministic`
(or a refactor that breaks the ``bind_overlay`` /
``env_overrides`` contract) would still leave both unit suites green;
this file fails loudly when that link breaks. The two test functions
mirror the two outcomes the wiring must guarantee:

  * Happy path: a materialized overlay reaches the validation
    subprocess env via ``PATH`` / ``PYTHONPATH``; resetting the
    overlay clears it again.
  * Failure path: a materialization failure surfaces as
    :class:`WorkerDepsMaterializationError` with the right
    ``stage`` tag (which the runner's step-6 step-4 emit path keys
    off — see :mod:`treadmill_agent.runner` and the
    ``task.worker_deps_failed`` event).

Because the test patches two different module boundaries in the same
test (the ``repo_deps`` install seam plus the ``validation_runtime``
script seam), it doubles as documentation for which module owns which
subprocess invocation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from treadmill_agent import repo_deps
from treadmill_agent.repo_deps import (
    WorkerDepsMaterializationError,
    bind_overlay,
    materialize,
    reset_overlay,
)
from treadmill_agent.validation_runtime import run_deterministic
from treadmill_api.models.onboarding import WorkerDeps


def _check(script: str = "exit 0") -> MagicMock:
    return MagicMock(
        id="check-wiring",
        kind="deterministic",
        severity="blocking",
        script=script,
    )


def test_wiring_overlay_env_reaches_validation_subprocess(
    tmp_path: Path,
) -> None:
    """A materialized overlay bound via :func:`bind_overlay` must reach
    :func:`run_deterministic`'s subprocess env. After
    :func:`reset_overlay`, the next invocation must NOT see the
    overlay paths — the ContextVar handoff is bidirectional or it
    isn't a handoff at all.

    Two patch contexts (one per module boundary) so the install-phase
    subprocess never collides with the task-phase one; that mirrors
    the way the two seams actually live in production.
    """
    worker_deps = WorkerDeps(python=["packaging==24.0"])

    with patch("treadmill_agent.repo_deps.subprocess.run") as install_run:
        install_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        overlay = materialize(
            "test/repo", worker_deps, overlay_root=tmp_path,
        )

    assert overlay.venv_path is not None
    assert overlay.fresh is True
    # Make the venv site-packages directory exist so
    # ``env_overrides()`` populates PYTHONPATH (the helper requires the
    # venv dir on disk because the unit-test mocks never let it create
    # one).
    site_packages = overlay.venv_path / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)

    token = bind_overlay(overlay)
    try:
        with patch(
            "treadmill_agent.validation_runtime.subprocess.run"
        ) as validate_run:
            validate_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="",
            )
            run_deterministic(_check(), tmp_path, timeout_seconds=5)

        env = validate_run.call_args.kwargs["env"]
        assert env["PATH"].startswith(str(overlay.venv_path / "bin")), (
            f"validation subprocess env did not pick up the overlay venv: "
            f"PATH={env.get('PATH')!r}"
        )
        assert str(site_packages) in env.get("PYTHONPATH", ""), (
            f"validation subprocess env did not pick up site-packages: "
            f"PYTHONPATH={env.get('PYTHONPATH')!r}"
        )
    finally:
        reset_overlay(token)

    # After reset, the next validation subprocess should NOT see the
    # overlay paths — the ContextVar reset path is part of the
    # contract, not a nice-to-have.
    with patch(
        "treadmill_agent.validation_runtime.subprocess.run"
    ) as validate_run_after:
        validate_run_after.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        run_deterministic(_check(), tmp_path, timeout_seconds=5)

    env_after = validate_run_after.call_args.kwargs["env"]
    assert str(overlay.venv_path / "bin") not in env_after.get("PATH", ""), (
        "validation subprocess saw overlay PATH after reset_overlay — "
        "ContextVar handoff leaked across steps"
    )
    assert str(site_packages) not in env_after.get("PYTHONPATH", ""), (
        "validation subprocess saw overlay PYTHONPATH after reset_overlay"
    )


def test_wiring_failure_propagates_as_materialization_error(
    tmp_path: Path,
) -> None:
    """A ``pip install`` failure inside :func:`materialize` must raise
    :class:`WorkerDepsMaterializationError` tagged ``stage='python'``
    — the runner's step-4 emit path (``task.worker_deps_failed``)
    keys off that ``stage`` attribute, so a refactor that swallowed
    the typed error or dropped the stage tag would silently lose
    the operator-visible escalation signal.
    """
    worker_deps = WorkerDeps(python=["packaging==24.0"])

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0]
        assert isinstance(cmd, list)
        if cmd[:3] == ["python", "-m", "venv"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr="",
            )
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd,
            output="", stderr="ERROR: simulated install failure",
        )

    with patch(
        "treadmill_agent.repo_deps.subprocess.run", side_effect=_fake_run,
    ):
        with pytest.raises(WorkerDepsMaterializationError) as exc_info:
            materialize("test/repo", worker_deps, overlay_root=tmp_path)

    assert exc_info.value.stage == "python"
    assert "simulated install failure" in exc_info.value.detail
    # The module-level alias is the seam the runner imports from; pin
    # that the typed-error shape is reachable via the public surface
    # too, not just the direct import in this test.
    assert isinstance(exc_info.value, repo_deps.WorkerDepsMaterializationError)
