"""Tests for treadmill schedules CLI commands."""

from __future__ import annotations

import json
import uuid

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from treadmill_cli.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREADMILL_API_URL", "http://fake-api")


def _schedule_payload(**overrides: object) -> dict:
    base: dict = {
        "id": str(uuid.uuid4()),
        "cron_expression": "0 9 * * *",
        "workflow_id": "wf-test",
        "payload_template": {},
        "status": "active",
        "jitter_seconds": 60,
        "quiet_hours": None,
        "quiet_tz": "America/Los_Angeles",
        "last_fired_at": None,
        "created_by": "operator",
        "created_at": "2026-05-17T00:00:00Z",
        "next_fire_at": "2026-05-18T09:00:00Z",
    }
    base.update(overrides)
    return base


# ── list ──────────────────────────────────────────────────────────────────────


def test_schedules_list_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/schedules",
        json=[],
        status_code=200,
    )
    result = runner.invoke(app, ["schedules", "list"])
    assert result.exit_code == 0, result.output
    assert "no schedules" in result.output


def test_schedules_list_shows_table(httpx_mock: HTTPXMock) -> None:
    s1 = _schedule_payload(cron_expression="0 9 * * *", workflow_id="wf-one")
    s2 = _schedule_payload(
        cron_expression="*/30 * * * *",
        workflow_id="wf-two",
        status="paused",
        next_fire_at=None,
    )
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/schedules",
        json=[s1, s2],
        status_code=200,
    )
    result = runner.invoke(app, ["schedules", "list"])
    assert result.exit_code == 0, result.output
    assert "wf-one" in result.output
    assert "wf-two" in result.output
    assert "active" in result.output
    assert "paused" in result.output
    assert "2026-05-18 09:00" in result.output


def test_schedules_list_api_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/schedules",
        json={"detail": "unauthorized"},
        status_code=401,
    )
    result = runner.invoke(app, ["schedules", "list"])
    assert result.exit_code == 2
    assert "401" in result.output


# ── create ────────────────────────────────────────────────────────────────────


def test_schedules_create_minimal(httpx_mock: HTTPXMock) -> None:
    s = _schedule_payload(cron_expression="0 8 * * 1", workflow_id="wf-weekly")
    httpx_mock.add_response(
        method="POST",
        url="http://fake-api/api/v1/schedules",
        json=s,
        status_code=201,
    )
    result = runner.invoke(app, ["schedules", "create", "0 8 * * 1", "wf-weekly"])
    assert result.exit_code == 0, result.output
    assert "created" in result.output
    assert s["id"] in result.output


def test_schedules_create_sends_correct_body(httpx_mock: HTTPXMock) -> None:
    s = _schedule_payload()
    httpx_mock.add_response(
        method="POST",
        url="http://fake-api/api/v1/schedules",
        json=s,
        status_code=201,
    )
    runner.invoke(app, [
        "schedules", "create", "*/5 * * * *", "wf-check",
        "--jitter", "120",
        "--quiet-hours", "22-6",
        "--quiet-tz", "UTC",
        "--payload", '{"env": "prod"}',
        "--created-by", "ci-bot",
    ])
    post = next(
        r for r in httpx_mock.get_requests()
        if r.method == "POST"
    )
    body = json.loads(post.content)
    assert body["cron_expression"] == "*/5 * * * *"
    assert body["workflow_id"] == "wf-check"
    assert body["jitter_seconds"] == 120
    assert body["quiet_hours"] == "22-6"
    assert body["quiet_tz"] == "UTC"
    assert body["payload_template"] == {"env": "prod"}
    assert body["created_by"] == "ci-bot"


def test_schedules_create_invalid_payload_json() -> None:
    result = runner.invoke(app, [
        "schedules", "create", "0 9 * * *", "wf-test",
        "--payload", "not-json",
    ])
    assert result.exit_code == 2
    assert "not valid JSON" in result.output


def test_schedules_create_payload_non_object() -> None:
    result = runner.invoke(app, [
        "schedules", "create", "0 9 * * *", "wf-test",
        "--payload", "[1, 2, 3]",
    ])
    assert result.exit_code == 2
    assert "JSON object" in result.output


def test_schedules_create_api_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://fake-api/api/v1/schedules",
        json={"detail": "workflow not found"},
        status_code=400,
    )
    result = runner.invoke(app, ["schedules", "create", "0 9 * * *", "wf-missing"])
    assert result.exit_code == 2
    assert "400" in result.output


# ── pause ─────────────────────────────────────────────────────────────────────


def test_schedules_pause(httpx_mock: HTTPXMock) -> None:
    sid = str(uuid.uuid4())
    s = _schedule_payload(id=sid, status="paused")
    httpx_mock.add_response(
        method="PATCH",
        url=f"http://fake-api/api/v1/schedules/{sid}",
        json=s,
        status_code=200,
    )
    result = runner.invoke(app, ["schedules", "pause", sid])
    assert result.exit_code == 0, result.output
    assert "paused" in result.output
    assert sid in result.output


def test_schedules_pause_sends_status_paused(httpx_mock: HTTPXMock) -> None:
    sid = str(uuid.uuid4())
    httpx_mock.add_response(
        method="PATCH",
        url=f"http://fake-api/api/v1/schedules/{sid}",
        json=_schedule_payload(id=sid, status="paused"),
        status_code=200,
    )
    runner.invoke(app, ["schedules", "pause", sid])
    patch = httpx_mock.get_requests()[0]
    body = json.loads(patch.content)
    assert body["status"] == "paused"


def test_schedules_pause_not_found(httpx_mock: HTTPXMock) -> None:
    sid = str(uuid.uuid4())
    httpx_mock.add_response(
        method="PATCH",
        url=f"http://fake-api/api/v1/schedules/{sid}",
        json={"detail": "schedule not found"},
        status_code=404,
    )
    result = runner.invoke(app, ["schedules", "pause", sid])
    assert result.exit_code == 2
    assert "404" in result.output


# ── resume ────────────────────────────────────────────────────────────────────


def test_schedules_resume(httpx_mock: HTTPXMock) -> None:
    sid = str(uuid.uuid4())
    s = _schedule_payload(id=sid, status="active")
    httpx_mock.add_response(
        method="PATCH",
        url=f"http://fake-api/api/v1/schedules/{sid}",
        json=s,
        status_code=200,
    )
    result = runner.invoke(app, ["schedules", "resume", sid])
    assert result.exit_code == 0, result.output
    assert "resumed" in result.output
    assert sid in result.output


def test_schedules_resume_sends_status_active(httpx_mock: HTTPXMock) -> None:
    sid = str(uuid.uuid4())
    httpx_mock.add_response(
        method="PATCH",
        url=f"http://fake-api/api/v1/schedules/{sid}",
        json=_schedule_payload(id=sid, status="active"),
        status_code=200,
    )
    runner.invoke(app, ["schedules", "resume", sid])
    patch = httpx_mock.get_requests()[0]
    body = json.loads(patch.content)
    assert body["status"] == "active"


# ── delete ────────────────────────────────────────────────────────────────────


def test_schedules_delete_with_yes_flag(httpx_mock: HTTPXMock) -> None:
    sid = str(uuid.uuid4())
    httpx_mock.add_response(
        method="DELETE",
        url=f"http://fake-api/api/v1/schedules/{sid}",
        status_code=204,
    )
    result = runner.invoke(app, ["schedules", "delete", sid, "--yes"])
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output
    assert sid in result.output


def test_schedules_delete_aborted_without_yes() -> None:
    sid = str(uuid.uuid4())
    # CliRunner feeds "n" as stdin
    result = runner.invoke(app, ["schedules", "delete", sid], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.output


def test_schedules_delete_confirmed_via_prompt(httpx_mock: HTTPXMock) -> None:
    sid = str(uuid.uuid4())
    httpx_mock.add_response(
        method="DELETE",
        url=f"http://fake-api/api/v1/schedules/{sid}",
        status_code=204,
    )
    result = runner.invoke(app, ["schedules", "delete", sid], input="y\n")
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output


def test_schedules_delete_not_found(httpx_mock: HTTPXMock) -> None:
    sid = str(uuid.uuid4())
    httpx_mock.add_response(
        method="DELETE",
        url=f"http://fake-api/api/v1/schedules/{sid}",
        json={"detail": "schedule not found"},
        status_code=404,
    )
    result = runner.invoke(app, ["schedules", "delete", sid, "--yes"])
    assert result.exit_code == 2
    assert "404" in result.output
