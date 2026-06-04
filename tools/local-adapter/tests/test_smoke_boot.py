"""Flag-parsing tests for ``tools/local-adapter/scripts/smoke_boot.sh``.

The script's main side effects (running ``treadmill-local up``,
polling /healthz, invoking ``start_worker_once``) all require a working
Docker daemon — the worker sandbox does not have one — so these tests
cover only the surface the sandbox CAN exercise: flag parsing, env
export, and downstream-command argument passthrough.

A stub ``treadmill-local`` is injected via ``SMOKE_TREADMILL_CMD``.
The stub records its argv and selected env vars to a file so each test
can assert what the script handed it.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "smoke_boot.sh"
)


def _write_stub(tmp_path: Path) -> tuple[Path, Path]:
    """Write a fake ``treadmill-local`` that records args + env, exits 0."""
    record = tmp_path / "record.txt"
    stub = tmp_path / "fake-treadmill-local"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "args:$*" >> {record}\n'
        f'echo "proxy:${{TREADMILL_EGRESS_PROXY_ENABLED:-}}" >> {record}\n'
        "exit 0\n"
    )
    mode = stub.stat().st_mode
    stub.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub, record


def _run(
    args: list[str],
    *,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_script_exists_and_is_a_file() -> None:
    """Guard against accidental deletion / rename."""
    assert SCRIPT.is_file(), f"missing script: {SCRIPT}"


def test_help_lists_all_flags() -> None:
    result = _run(["--help"])
    assert result.returncode == 0, result.stderr
    for flag in ("--port", "--proxy-enabled", "--timeout"):
        assert flag in result.stdout, result.stdout


def test_unknown_flag_exits_nonzero() -> None:
    result = _run(["--bogus"])
    assert result.returncode != 0
    # Usage hint + the offending arg surface on stderr.
    combined = (result.stdout + result.stderr).lower()
    assert "unknown" in combined or "usage" in combined


def test_defaults_pass_required_flags_to_treadmill_local(tmp_path: Path) -> None:
    stub, record = _write_stub(tmp_path)
    result = _run(
        ["--timeout", "1"],
        env_overrides={"SMOKE_TREADMILL_CMD": str(stub)},
    )
    # The sandbox has no real API listening, so /healthz polling
    # fails and the script exits BOOT_FAILED. That's the expected
    # sentinel — what we check is the args the stub received.
    assert "BOOT_FAILED" in result.stdout, result.stdout
    assert result.returncode == 1
    recorded = record.read_text()
    expected = (
        "args:up --no-build --no-autoscaler --no-scheduler --no-observability"
    )
    assert expected in recorded, recorded


def test_default_proxy_enabled_true_exported(tmp_path: Path) -> None:
    stub, record = _write_stub(tmp_path)
    _run(
        ["--timeout", "1"],
        env_overrides={"SMOKE_TREADMILL_CMD": str(stub)},
    )
    assert "proxy:true" in record.read_text()


def test_proxy_enabled_can_be_disabled(tmp_path: Path) -> None:
    stub, record = _write_stub(tmp_path)
    _run(
        ["--proxy-enabled", "false", "--timeout", "1"],
        env_overrides={"SMOKE_TREADMILL_CMD": str(stub)},
    )
    assert "proxy:false" in record.read_text()


def test_custom_port_used_in_healthz_url(tmp_path: Path) -> None:
    stub, _ = _write_stub(tmp_path)
    result = _run(
        ["--port", "9999", "--timeout", "1"],
        env_overrides={"SMOKE_TREADMILL_CMD": str(stub)},
    )
    assert "localhost:9999/healthz" in result.stdout
