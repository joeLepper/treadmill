"""Tests for `treadmill task retry` command."""

from __future__ import annotations

import uuid

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from treadmill_cli.cli import app


runner = CliRunner()

TASK_ID = str(uuid.uuid4())
RUN_ID = str(uuid.uuid4())
_RETRY_URL = f"http://fake-api/api/v1/tasks/{TASK_ID}/retry"


@pytest.fixture(autouse=True)
def _api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREADMILL_API_URL", "http://fake-api")


def test_task_retry_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_RETRY_URL,
        json={"workflow_run_id": RUN_ID},
        status_code=201,
    )
    result = runner.invoke(
        app, ["task", "retry", TASK_ID, "--reason", "re-run after flake"],
    )
    assert result.exit_code == 0, result.output
    assert f"retry dispatched: workflow_run={RUN_ID}" in result.output


def test_task_retry_with_workflow(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_RETRY_URL,
        json={"workflow_run_id": RUN_ID},
        status_code=201,
    )
    result = runner.invoke(
        app,
        ["task", "retry", TASK_ID, "--reason", "manual", "--workflow", "wf-author"],
    )
    assert result.exit_code == 0, result.output
    assert RUN_ID in result.output
    req = httpx_mock.get_requests()[0]
    import json
    body = json.loads(req.content)
    # API's TaskRetryRequest schema uses ``workflow_id``; the previous
    # assertion (``body["workflow"]``) mirrored a CLI bug that made
    # ``--workflow`` a no-op for terminal-task retries.
    assert body["workflow_id"] == "wf-author"
    assert "workflow" not in body, "regression: old wrong field name resurfaced"


def test_task_retry_with_force_bypass_cap(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_RETRY_URL,
        json={"workflow_run_id": RUN_ID},
        status_code=201,
    )
    result = runner.invoke(
        app,
        ["task", "retry", TASK_ID, "--reason", "force it", "--force-bypass-cap"],
    )
    assert result.exit_code == 0, result.output
    req = httpx_mock.get_requests()[0]
    import json
    body = json.loads(req.content)
    assert body["force_bypass_cap"] is True


def test_task_retry_missing_reason_rejected_by_typer() -> None:
    result = runner.invoke(app, ["task", "retry", TASK_ID])
    assert result.exit_code == 2
    assert "reason" in result.output.lower() or "missing" in result.output.lower()


def test_task_retry_409_cap_reached(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_RETRY_URL,
        json={"detail": "attempt cap reached"},
        status_code=409,
    )
    result = runner.invoke(
        app, ["task", "retry", TASK_ID, "--reason", "try again"],
    )
    assert result.exit_code == 2
    assert "cap reached" in result.output
    assert "--force-bypass-cap" in result.output


def test_task_retry_404_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_RETRY_URL,
        json={"detail": "not found"},
        status_code=404,
    )
    result = runner.invoke(
        app, ["task", "retry", TASK_ID, "--reason", "try again"],
    )
    assert result.exit_code == 2
    assert "task not found" in result.output


def test_task_retry_other_api_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_RETRY_URL,
        json={"detail": "internal server error"},
        status_code=500,
    )
    result = runner.invoke(
        app, ["task", "retry", TASK_ID, "--reason", "try again"],
    )
    assert result.exit_code == 2
    assert "500" in result.output
