"""Tests for plan submit picking up TREADMILL_SESSION_LABEL as created_by."""

from __future__ import annotations

import json
import uuid

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from treadmill_cli.cli import app
from treadmill_cli.identity import SESSION_LABEL_ENV

runner = CliRunner()


@pytest.fixture(autouse=True)
def _api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREADMILL_API_URL", "http://fake-api")


def _plan_payload(**overrides) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "repo": "test/repo",
        "intent": "do thing",
        "doc_path": None,
        "parent_plan_id": None,
        "created_by": None,
        "created_at": "2026-06-04T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_plan_submit_picks_up_session_label_when_no_explicit(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TREADMILL_SESSION_LABEL is set and --created-by is omitted,
    plan submit must send created_by equal to the session label."""
    monkeypatch.setenv(SESSION_LABEL_ENV, "treadmill-bert")
    plan = _plan_payload(created_by="treadmill-bert")
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json=plan, status_code=201,
    )

    result = runner.invoke(app, ["plan", "submit", "-r", "test/repo", "-i", "do thing"])
    assert result.exit_code == 0, result.output

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["created_by"] == "treadmill-bert"
