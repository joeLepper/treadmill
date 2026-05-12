"""End-to-end CLI tests against the live API.

Skipped by default; opt in with ``TREADMILL_INTEGRATION=1``. Requires
``treadmill-local up`` in the parent project.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
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


@pytest.fixture(scope="module")
def api_url() -> str:
    return os.environ.get("TREADMILL_API_URL", DEFAULT_API_URL)


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Engine:
    eng = sa.create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


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


_TEST_TABLES = (
    "plans", "tasks", "task_prs", "task_dependencies",
    "workflow_runs", "workflow_run_steps", "events",
    "event_triggers",
    "workflows", "workflow_versions", "workflow_version_steps",
    "roles", "skills", "hooks",
)


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    def _do() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(_TEST_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )
    _do()
    yield
    _do()


@pytest.fixture
def seed_workflow(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO workflows (id) VALUES ('wf-author')"))
        conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES ('wf-author', 1)"
        ))
        conn.execute(sa.text(
            "INSERT INTO roles (id, model, system_prompt) "
            "VALUES ('role-author', 'claude', 'be a coder')"
        ))


@pytest.fixture
def runner(api_url: str, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("TREADMILL_API_URL", api_url)
    return CliRunner()


# ── End-to-end paths ─────────────────────────────────────────────────────────


def test_status_against_live_api(runner: CliRunner) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "liveness" in result.output


def test_plan_submit_intent_then_show(runner: CliRunner, truncate: None) -> None:
    submit = runner.invoke(app, [
        "plan", "submit", "-r", "cli-test/repo", "-i", "build a thing",
    ])
    assert submit.exit_code == 0, submit.output
    plan_id = next(
        line.split()[-1].strip("[bold]").rstrip("[/bold]")
        for line in submit.output.splitlines()
        if "plan created" in line
    ).strip()

    show = runner.invoke(app, ["plan", "show", plan_id])
    assert show.exit_code == 0
    assert plan_id in show.output


def test_submit_creates_plan_and_task_end_to_end(
    runner: CliRunner, truncate: None, seed_workflow: None,
) -> None:
    result = runner.invoke(app, [
        "submit", "fix the OAuth redirect", "-r", "cli-test/repo",
    ])
    assert result.exit_code == 0, result.output
    assert "submitted" in result.output


def test_task_list_returns_tasks_after_submit(
    runner: CliRunner, truncate: None, seed_workflow: None,
) -> None:
    runner.invoke(app, ["submit", "task A", "-r", "cli-test/repo"])
    runner.invoke(app, ["submit", "task B", "-r", "cli-test/repo"])
    listed = runner.invoke(app, ["task", "list", "-r", "cli-test/repo"])
    assert listed.exit_code == 0
    assert "Tasks (2)" in listed.output
