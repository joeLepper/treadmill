"""Tests for ``treadmill promote`` CLI commands (ADR-0088).

httpx_mock for the HTTP layer, CliRunner for invocation. The
workflow-dispatch hop (``gh workflow run``) is patched at the
``subprocess.run`` seam — the asserted properties are the ORDER
(approval recorded before dispatch), the key header presence, and the
recorded-but-dispatch-failed messaging, none of which the patch fakes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from treadmill_cli.cli import app

runner = CliRunner()

KEY = "test-operator-key"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREADMILL_API_URL", "http://fake-api")
    monkeypatch.setenv("TREADMILL_OPERATOR_KEY", KEY)


def _row(status: str = "proposed") -> dict:
    pid = str(uuid.uuid4())
    return {
        "proposal_id": pid,
        "repo": "acme/widget",
        "status": status,
        "bundle": {
            "env_from": "staging",
            "env_to": "prod",
            "digests": [{"service": "api", "digest": "sha256:" + "a" * 64}],
            "staging_evidence": {"sha": "b" * 40, "smoke_passed_at": "2026-06-11T00:00:00Z"},
            "diff_summary": ["#101", "#102"],
            "diff_anchor": "genesis:" + "b" * 40,
            "proposed_by": "coordinator-acme-widget",
        },
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
        "decided_by": "joe" if status in ("approved", "rejected") else None,
        "decided_at": None,
        "decision_note": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def test_list_renders_table(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://fake-api/api/v1/prod_promotions", json=[_row()]
    )
    result = runner.invoke(app, ["promote", "list"])
    assert result.exit_code == 0
    assert "acme/widget" in result.output
    assert "proposed" in result.output


def test_show_prints_diff_summary(httpx_mock: HTTPXMock) -> None:
    row = _row()
    httpx_mock.add_response(
        url=f"http://fake-api/api/v1/prod_promotions/{row['proposal_id']}",
        json=row,
    )
    result = runner.invoke(app, ["promote", "show", row["proposal_id"]])
    assert result.exit_code == 0
    assert "#101" in result.output
    assert "sha256:" in result.output
    assert "genesis:" in result.output


def test_approve_sends_key_then_dispatches(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row("approved")
    httpx_mock.add_response(
        url=f"http://fake-api/api/v1/prod_promotions/{row['proposal_id']}/approve",
        json=row,
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr("treadmill_cli.commands.promote.subprocess.run", fake_run)
    result = runner.invoke(app, ["promote", "approve", row["proposal_id"]])
    assert result.exit_code == 0
    # The keyed API call happened (and carried the header).
    request = httpx_mock.get_requests()[0]
    assert request.headers["X-Operator-Key"] == KEY
    # The dispatch fired after, against the proposal's repo + id.
    assert len(calls) == 1
    assert "promote-to-prod.yml" in calls[0]
    assert f"proposal_id={row['proposal_id']}" in calls[0]
    assert "acme/widget" in calls[0]


def test_approve_without_key_refuses_before_any_call(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TREADMILL_OPERATOR_KEY")
    result = runner.invoke(app, ["promote", "approve", str(uuid.uuid4())])
    assert result.exit_code == 2
    assert "TREADMILL_OPERATOR_KEY" in result.output
    assert not httpx_mock.get_requests()  # refused client-side, no API call


def test_approve_dispatch_failure_reports_recorded(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row("approved")
    httpx_mock.add_response(
        url=f"http://fake-api/api/v1/prod_promotions/{row['proposal_id']}/approve",
        json=row,
    )

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stderr = "gh: workflow not found"

        return R()

    monkeypatch.setattr("treadmill_cli.commands.promote.subprocess.run", fake_run)
    result = runner.invoke(app, ["promote", "approve", row["proposal_id"]])
    assert result.exit_code == 3
    assert "approval IS recorded" in result.output
    assert "Re-run" in result.output


def test_approve_no_dispatch_flag(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row("approved")
    httpx_mock.add_response(
        url=f"http://fake-api/api/v1/prod_promotions/{row['proposal_id']}/approve",
        json=row,
    )

    def boom(cmd, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("dispatch must not fire under --no-dispatch")

    monkeypatch.setattr("treadmill_cli.commands.promote.subprocess.run", boom)
    result = runner.invoke(
        app, ["promote", "approve", row["proposal_id"], "--no-dispatch"]
    )
    assert result.exit_code == 0
    assert "dispatch skipped" in result.output


def test_reject_requires_reason_and_sends_key(httpx_mock: HTTPXMock) -> None:
    row = _row("rejected")
    httpx_mock.add_response(
        url=f"http://fake-api/api/v1/prod_promotions/{row['proposal_id']}/reject",
        json=row,
    )
    result = runner.invoke(
        app,
        ["promote", "reject", row["proposal_id"], "--reason", "stale evidence"],
    )
    assert result.exit_code == 0
    request = httpx_mock.get_requests()[0]
    assert request.headers["X-Operator-Key"] == KEY

    # Missing --reason is a usage error from typer (required option).
    result = runner.invoke(app, ["promote", "reject", str(uuid.uuid4())])
    assert result.exit_code != 0


def test_api_409_surfaces_cleanly(httpx_mock: HTTPXMock) -> None:
    pid = str(uuid.uuid4())
    httpx_mock.add_response(
        url=f"http://fake-api/api/v1/prod_promotions/{pid}/approve",
        status_code=409,
        json={"detail": "proposal is expired, not approvable"},
    )
    result = runner.invoke(app, ["promote", "approve", pid, "--no-dispatch"])
    assert result.exit_code == 2
    assert "expired" in result.output
