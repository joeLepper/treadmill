"""End-to-end test for ``treadmill workflows seed-starters`` against the
live API.

Skipped by default; opt in with ``TREADMILL_INTEGRATION=1``. Requires
``treadmill-local up``. Exercises idempotency: a second run with all
seven workflows already present must complete without error.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from treadmill_cli.cli import app

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


DEFAULT_API_URL = "http://localhost:8088"
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)


_EXPECTED_WORKFLOWS = {
    "wf-plan",
    "wf-author",
    "wf-review",
    "wf-validate",
    "wf-feedback",
    "wf-ci-fix",
    "wf-conflict",
}


@pytest.fixture(scope="module")
def api_url() -> str:
    return os.environ.get("TREADMILL_API_URL", DEFAULT_API_URL)


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str) -> None:
    services_api_dir = (
        Path(__file__).resolve().parents[2] / "services" / "api"
    )
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


@pytest.fixture(scope="module", autouse=True)
def wait_for_api(api_url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"API not reachable at {api_url}")


@pytest.fixture
def runner(api_url: str, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("TREADMILL_API_URL", api_url)
    return CliRunner()


def _existing_workflow_ids(api_url: str) -> set[str]:
    response = httpx.get(f"{api_url}/api/v1/workflows", timeout=5.0)
    response.raise_for_status()
    return {wf["id"] for wf in response.json()}


def test_seed_starters_creates_all_seven(runner: CliRunner, api_url: str) -> None:
    """First-time seed populates every canonical workflow."""
    # Don't assume the substrate was just brought up — record before /
    # after so the test is robust against re-runs.
    before = _existing_workflow_ids(api_url)

    result = runner.invoke(app, ["workflows", "seed-starters"])
    assert result.exit_code == 0, result.output

    after = _existing_workflow_ids(api_url)
    missing = _EXPECTED_WORKFLOWS - after
    assert not missing, (
        f"after seed-starters, missing: {missing} (had before: {before & _EXPECTED_WORKFLOWS})"
    )


def test_seed_starters_is_idempotent(runner: CliRunner, api_url: str) -> None:
    """Second run must complete with exit code 0 — 409s are swallowed."""
    # Run once to guarantee everything is seeded.
    runner.invoke(app, ["workflows", "seed-starters"])

    # Run again — must not error.
    second = runner.invoke(app, ["workflows", "seed-starters"])
    assert second.exit_code == 0, second.output

    # Every canonical workflow still present.
    after = _existing_workflow_ids(api_url)
    missing = _EXPECTED_WORKFLOWS - after
    assert not missing, f"missing after idempotent re-run: {missing}"
