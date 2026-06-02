"""Tests for ``treadmill escalations`` CLI commands (ADR-0062 Step 3).

Mirrors ``test_schedules_command.py`` in shape — httpx_mock for the
HTTP layer, typer's CliRunner for command invocation. The ``tail``
subcommand opens a streaming response; we exercise its bootstrap call
and a single SSE frame through ``httpx_mock``'s streamed-body support.
"""

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


def _open_row(
    *,
    task_id: str | None = None,
    repo: str = "joeLepper/treadmill",
    title: str = "fix the thing",
    opened_at: str = "2026-06-02T14:00:00+00:00",
    reason: str | None = None,
) -> dict:
    return {
        "task_id": task_id or str(uuid.uuid4()),
        "repo": repo,
        "title": title,
        "opened_at": opened_at,
        "reason": reason,
    }


# ── list ──────────────────────────────────────────────────────────────────────


def test_escalations_list_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations",
        json=[],
        status_code=200,
    )
    result = runner.invoke(app, ["escalations", "list"])
    assert result.exit_code == 0, result.output
    assert "no open escalations" in result.output


def test_escalations_list_shows_table(httpx_mock: HTTPXMock) -> None:
    rows = [
        _open_row(reason="architect_cap", repo="org/one"),
        _open_row(reason="stuck_task_sweep", repo="org/two"),
    ]
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations",
        json=rows,
        status_code=200,
    )
    result = runner.invoke(app, ["escalations", "list"])
    assert result.exit_code == 0, result.output
    assert "architect_cap" in result.output
    assert "stuck_task_sweep" in result.output
    assert "org/one" in result.output
    assert "org/two" in result.output


def test_escalations_list_passes_reason_filter(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations?reason=gate-broken",
        json=[],
        status_code=200,
    )
    result = runner.invoke(
        app, ["escalations", "list", "--reason", "gate-broken"],
    )
    assert result.exit_code == 0, result.output
    request = httpx_mock.get_requests()[0]
    assert b"reason=gate-broken" in request.url.query


def test_escalations_list_passes_task_prefix(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations?task=abc",
        json=[],
        status_code=200,
    )
    result = runner.invoke(
        app, ["escalations", "list", "--task", "abc"],
    )
    assert result.exit_code == 0, result.output
    request = httpx_mock.get_requests()[0]
    assert b"task=abc" in request.url.query


def test_escalations_list_api_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations",
        json={"detail": "unauthorized"},
        status_code=401,
    )
    result = runner.invoke(app, ["escalations", "list"])
    assert result.exit_code == 2
    assert "401" in result.output


# ── close ─────────────────────────────────────────────────────────────────────


def test_escalations_close_happy_path(httpx_mock: HTTPXMock) -> None:
    task_id = str(uuid.uuid4())
    httpx_mock.add_response(
        method="POST",
        url=f"http://fake-api/api/v1/escalations/{task_id}/close",
        json={
            "task_id": task_id,
            "close_reason": "operator_close",
            "mttr_seconds": 3720,
        },
        status_code=202,
    )
    result = runner.invoke(app, ["escalations", "close", task_id])
    assert result.exit_code == 0, result.output
    assert "closed" in result.output
    assert "operator_close" in result.output
    # 3720 seconds → 1h02m formatting.
    assert "1h02m" in result.output


def test_escalations_close_404(httpx_mock: HTTPXMock) -> None:
    task_id = str(uuid.uuid4())
    httpx_mock.add_response(
        method="POST",
        url=f"http://fake-api/api/v1/escalations/{task_id}/close",
        json={"detail": "task not found"},
        status_code=404,
    )
    result = runner.invoke(app, ["escalations", "close", task_id])
    assert result.exit_code == 2
    assert "404" in result.output


def test_escalations_close_409_no_open_incident(httpx_mock: HTTPXMock) -> None:
    task_id = str(uuid.uuid4())
    httpx_mock.add_response(
        method="POST",
        url=f"http://fake-api/api/v1/escalations/{task_id}/close",
        json={"detail": "task has no open escalation"},
        status_code=409,
    )
    result = runner.invoke(app, ["escalations", "close", task_id])
    assert result.exit_code == 2
    assert "409" in result.output


# ── ack ───────────────────────────────────────────────────────────────────────


def test_escalations_ack_happy_path(httpx_mock: HTTPXMock) -> None:
    task_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    httpx_mock.add_response(
        method="POST",
        url=f"http://fake-api/api/v1/escalations/{task_id}/ack",
        json={"task_id": task_id, "event_id": event_id},
        status_code=202,
    )
    result = runner.invoke(app, ["escalations", "ack", task_id])
    assert result.exit_code == 0, result.output
    assert "acked" in result.output
    assert event_id[:8] in result.output


def test_escalations_ack_409_not_escalated(httpx_mock: HTTPXMock) -> None:
    task_id = str(uuid.uuid4())
    httpx_mock.add_response(
        method="POST",
        url=f"http://fake-api/api/v1/escalations/{task_id}/ack",
        json={"detail": "task is not currently escalated"},
        status_code=409,
    )
    result = runner.invoke(app, ["escalations", "ack", task_id])
    assert result.exit_code == 2
    assert "409" in result.output


# ── report ────────────────────────────────────────────────────────────────────


def test_escalations_report_by_reason(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations/report?by=reason",
        json={
            "since": "2026-05-26T00:00:00+00:00",
            "by": "reason",
            "total": 3,
            "buckets": [
                {
                    "key": "operator_close",
                    "count": 2,
                    "mttr_seconds_avg": 600,
                    "mttr_seconds_p50": 600,
                    "mttr_seconds_p95": 900,
                },
                {
                    "key": "re_progressed",
                    "count": 1,
                    "mttr_seconds_avg": 300,
                    "mttr_seconds_p50": 300,
                    "mttr_seconds_p95": 300,
                },
            ],
        },
        status_code=200,
    )
    result = runner.invoke(app, ["escalations", "report"])
    assert result.exit_code == 0, result.output
    assert "operator_close" in result.output
    assert "re_progressed" in result.output


def test_escalations_report_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations/report?by=reason",
        json={
            "since": "2026-05-26T00:00:00+00:00",
            "by": "reason",
            "total": 0,
            "buckets": [],
        },
        status_code=200,
    )
    result = runner.invoke(app, ["escalations", "report"])
    assert result.exit_code == 0, result.output
    assert "no closed incidents" in result.output


def test_escalations_report_by_day_passes_param(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations/report?by=day",
        json={
            "since": "2026-05-26T00:00:00+00:00",
            "by": "day",
            "total": 0,
            "buckets": [],
        },
        status_code=200,
    )
    runner.invoke(app, ["escalations", "report", "--by", "day"])
    request = httpx_mock.get_requests()[0]
    assert b"by=day" in request.url.query


def test_escalations_report_passes_since(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=(
            "http://fake-api/api/v1/escalations/report"
            "?by=reason&since=2026-05-01T00%3A00%3A00%2B00%3A00"
        ),
        json={
            "since": "2026-05-01T00:00:00+00:00",
            "by": "reason",
            "total": 0,
            "buckets": [],
        },
        status_code=200,
    )
    result = runner.invoke(
        app,
        [
            "escalations", "report",
            "--since", "2026-05-01T00:00:00+00:00",
        ],
    )
    assert result.exit_code == 0, result.output


def test_escalations_report_rejects_bad_since() -> None:
    result = runner.invoke(
        app, ["escalations", "report", "--since", "yesterday"],
    )
    assert result.exit_code == 2
    assert "ISO timestamp" in result.output


def test_escalations_report_rejects_bad_by() -> None:
    result = runner.invoke(app, ["escalations", "report", "--by", "decade"])
    assert result.exit_code == 2
    assert "reason / day / task" in result.output


# ── tail ──────────────────────────────────────────────────────────────────────


def test_escalations_tail_snapshot_then_streams_events(httpx_mock: HTTPXMock) -> None:
    """``tail`` first GETs the snapshot, then connects to /stream and
    consumes SSE frames until the body ends."""
    task_id = str(uuid.uuid4())
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations",
        json=[_open_row(task_id=task_id, reason="architect_cap")],
        status_code=200,
    )

    # SSE response — a comment line, one data frame, then EOF.
    sse_body = (
        b": connected\n\n"
        + b'data: '
        + json.dumps(
            {
                "id": str(uuid.uuid4()),
                "entity_type": "task",
                "action": "escalation_closed",
                "task_id": task_id,
                "ts": "2026-06-02T14:00:00+00:00",
            }
        ).encode()
        + b"\n\n"
    )
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations/stream",
        content=sse_body,
        status_code=200,
        headers={"Content-Type": "text/event-stream"},
    )

    result = runner.invoke(app, ["escalations", "tail"])
    assert result.exit_code == 0, result.output
    assert "Open escalations" in result.output
    assert "architect_cap" in result.output
    # The SSE-data frame's action surfaces in the rendered line.
    assert "escalation_closed" in result.output


def test_escalations_tail_snapshot_empty_then_streams(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations",
        json=[],
        status_code=200,
    )
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations/stream",
        content=b"",
        status_code=200,
        headers={"Content-Type": "text/event-stream"},
    )
    result = runner.invoke(app, ["escalations", "tail"])
    assert result.exit_code == 0, result.output
    assert "no open escalations at start" in result.output


def test_escalations_tail_stream_connect_failure_reports_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations",
        json=[],
        status_code=200,
    )
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/escalations/stream",
        status_code=503,
        json={"detail": "stream not configured"},
    )
    result = runner.invoke(app, ["escalations", "tail"])
    assert result.exit_code == 2
    assert "stream connect failed" in result.output
