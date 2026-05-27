"""Tests for `treadmill workflows trigger` command (ADR-0053 Wave 3)."""

from __future__ import annotations

import json
import uuid

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from treadmill_cli.cli import app


runner = CliRunner()

RUN_ID = str(uuid.uuid4())
_TRIGGER_URL = "http://fake-api/api/v1/workflows/wf-tune-judge-prompts/trigger"


@pytest.fixture(autouse=True)
def _api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREADMILL_API_URL", "http://fake-api")


def test_workflows_trigger_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_TRIGGER_URL,
        json={"run_id": RUN_ID, "workflow_id": "wf-tune-judge-prompts"},
        status_code=201,
    )
    payload = json.dumps({"repo": "acme/example", "judge_role": "role-validator"})
    result = runner.invoke(
        app, ["workflows", "trigger", "wf-tune-judge-prompts", "--payload", payload],
    )
    assert result.exit_code == 0, result.output
    assert f"triggered: workflow_run={RUN_ID}" in result.output

    req = httpx_mock.get_requests()[0]
    body = json.loads(req.content)
    assert body == {
        "payload": {"repo": "acme/example", "judge_role": "role-validator"},
    }


def test_workflows_trigger_invalid_json_payload() -> None:
    result = runner.invoke(
        app,
        ["workflows", "trigger", "wf-tune-judge-prompts", "--payload", "not-json"],
    )
    assert result.exit_code == 2
    assert "invalid --payload JSON" in result.output


def test_workflows_trigger_non_object_payload() -> None:
    result = runner.invoke(
        app,
        ["workflows", "trigger", "wf-tune-judge-prompts", "--payload", "[1, 2, 3]"],
    )
    assert result.exit_code == 2
    assert "JSON object" in result.output


def test_workflows_trigger_404_workflow_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_TRIGGER_URL,
        json={"detail": "workflow 'wf-tune-judge-prompts' not found"},
        status_code=404,
    )
    result = runner.invoke(
        app,
        [
            "workflows", "trigger", "wf-tune-judge-prompts",
            "--payload", json.dumps({"repo": "acme/example"}),
        ],
    )
    assert result.exit_code == 2
    assert "workflow not found" in result.output


def test_workflows_trigger_400_missing_repo(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_TRIGGER_URL,
        json={"detail": "payload missing required field: repo"},
        status_code=400,
    )
    result = runner.invoke(
        app,
        [
            "workflows", "trigger", "wf-tune-judge-prompts",
            "--payload", json.dumps({"judge_role": "role-validator"}),
        ],
    )
    assert result.exit_code == 2
    assert "payload missing required field: repo" in result.output


def test_workflows_trigger_other_api_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_TRIGGER_URL,
        json={"detail": "internal server error"},
        status_code=500,
    )
    result = runner.invoke(
        app,
        [
            "workflows", "trigger", "wf-tune-judge-prompts",
            "--payload", json.dumps({"repo": "acme/example"}),
        ],
    )
    assert result.exit_code == 2
    assert "500" in result.output


def test_workflows_trigger_missing_payload_rejected_by_typer() -> None:
    result = runner.invoke(
        app, ["workflows", "trigger", "wf-tune-judge-prompts"],
    )
    assert result.exit_code == 2
