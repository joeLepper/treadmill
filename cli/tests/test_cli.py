"""CLI unit tests using pytest-httpx to mock API responses.

These tests cover argument validation + happy-path calls against a fake
API. End-to-end tests against the live API live in
``test_integration_cli.py`` (run only with TREADMILL_INTEGRATION=1).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from treadmill_cli.cli import app


runner = CliRunner()


@pytest.fixture(autouse=True)
def _api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Always point CLI at a fake URL so pytest-httpx can intercept."""
    monkeypatch.setenv("TREADMILL_API_URL", "http://fake-api")


def _plan_payload(plan_id: str | None = None, **overrides) -> dict:
    base = {
        "id": plan_id or str(uuid.uuid4()),
        "repo": "test/repo",
        "intent": "do thing",
        "doc_path": None,
        "parent_plan_id": None,
        "created_by": None,
        "created_at": "2026-05-08T00:00:00Z",
    }
    base.update(overrides)
    return base


def _task_payload(task_id: str | None = None, **overrides) -> dict:
    base = {
        "id": task_id or str(uuid.uuid4()),
        "plan_id": str(uuid.uuid4()),
        "repo": "test/repo",
        "title": "T",
        "description": "desc",
        "workflow_version_id": str(uuid.uuid4()),
        "created_by": None,
        "created_at": "2026-05-08T00:00:00Z",
        "derived_status": "registered",
    }
    base.update(overrides)
    return base


# ── plan submit ──────────────────────────────────────────────────────────────


def test_plan_submit_with_intent_calls_post_plans(httpx_mock: HTTPXMock) -> None:
    plan = _plan_payload()
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json=plan, status_code=201,
    )
    result = runner.invoke(app, ["plan", "submit", "-r", "test/repo", "-i", "do thing"])
    assert result.exit_code == 0, result.output
    assert "plan created" in result.output
    assert plan["id"] in result.output


def test_plan_submit_with_doc_reads_file_and_lists_tasks(
    httpx_mock: HTTPXMock, tmp_path: Path,
) -> None:
    """Doc submission lists spawned tasks after creating the plan."""
    plan = _plan_payload(doc_path=None)
    plan_id = plan["id"]
    doc_file = tmp_path / "plan.md"
    doc_file.write_text("# Plan\n\n## sequence_of_work\n\n```yaml\nx: 1\n```\n")

    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json={**plan, "doc_path": str(doc_file)}, status_code=201,
    )
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/plans/{plan_id}/tasks",
        json=[_task_payload(), _task_payload()],
    )

    result = runner.invoke(app, [
        "plan", "submit", "-r", "test/repo", "-d", str(doc_file),
    ])
    assert result.exit_code == 0, result.output
    assert "2 spawned" in result.output


def test_plan_submit_requires_one_of_doc_or_intent() -> None:
    result = runner.invoke(app, ["plan", "submit", "-r", "test/repo"])
    assert result.exit_code == 2
    assert "either --doc or --intent" in result.output


def test_plan_submit_rejects_both_doc_and_intent(tmp_path: Path) -> None:
    doc = tmp_path / "x.md"
    doc.write_text("x")
    result = runner.invoke(app, [
        "plan", "submit", "-r", "test/repo", "-i", "x", "-d", str(doc),
    ])
    assert result.exit_code == 2
    assert "use only one" in result.output


def test_plan_submit_with_missing_doc_file(tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "plan", "submit", "-r", "test/repo", "-d", str(tmp_path / "nope.md"),
    ])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_plan_submit_with_dev_propagates_flag_in_body(httpx_mock: HTTPXMock) -> None:
    """The --dev flag must travel as ``dev: true`` in the POST body so the
    API can short-circuit the wf-plan PR-merge gate (D.10). After create,
    the CLI lists the implicit wf-author task the API spawned."""
    plan = _plan_payload()
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json=plan, status_code=201,
    )
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/plans/{plan['id']}/tasks",
        json=[_task_payload(plan_id=plan["id"])],
    )

    result = runner.invoke(app, [
        "plan", "submit", "-r", "test/repo", "-i", "fix the redirect", "--dev",
    ])
    assert result.exit_code == 0, result.output
    # Locate the POST request and confirm the body carries dev: true.
    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert posts, "expected a POST /plans request"
    import json
    body = json.loads(posts[0].content)
    assert body.get("dev") is True
    assert body.get("intent") == "fix the redirect"
    # The CLI also lists tasks under the dev-spawned plan.
    assert "1 spawned" in result.output


def test_submit_shorthand_with_dev_skips_followup_task_post(
    httpx_mock: HTTPXMock,
) -> None:
    """The ``submit`` shorthand normally POSTs /plans then POSTs /tasks
    to create the implicit task. With --dev the API spawns the task
    inline, so the CLI must skip the second POST and instead GET the
    plan's tasks to report the result."""
    plan = _plan_payload()
    task = _task_payload(plan_id=plan["id"])
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json=plan, status_code=201,
    )
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/plans/{plan['id']}/tasks",
        json=[task],
    )

    result = runner.invoke(app, [
        "submit", "fix the redirect", "-r", "test/repo", "--dev",
    ])
    assert result.exit_code == 0, result.output
    # Exactly one POST (the plan); no POST /tasks should have fired.
    requests_by_method = {(r.method, r.url.path) for r in httpx_mock.get_requests()}
    assert ("POST", "/api/v1/plans") in requests_by_method
    assert ("POST", "/api/v1/tasks") not in requests_by_method
    # Body propagates dev: true.
    import json
    [post_req] = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert json.loads(post_req.content).get("dev") is True
    # Output shows the API-spawned task.
    assert plan["id"] in result.output
    assert task["id"] in result.output


def test_plan_submit_surfaces_api_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json={"detail": "boom"}, status_code=400,
    )
    result = runner.invoke(app, ["plan", "submit", "-r", "test/repo", "-i", "x"])
    assert result.exit_code == 2
    assert "error 400" in result.output


# ── plan show ────────────────────────────────────────────────────────────────


def test_plan_show_prints_plan_and_tasks(httpx_mock: HTTPXMock) -> None:
    plan = _plan_payload(plan_id="aaaaaaaa-0000-0000-0000-000000000000")
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/plans/{plan['id']}",
        json=plan,
    )
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/plans/{plan['id']}/tasks",
        json=[_task_payload(), _task_payload()],
    )
    result = runner.invoke(app, ["plan", "show", plan["id"]])
    assert result.exit_code == 0
    assert plan["id"] in result.output
    assert "Tasks (2)" in result.output


def test_plan_show_handles_no_tasks(httpx_mock: HTTPXMock) -> None:
    plan = _plan_payload()
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/plans/{plan['id']}", json=plan,
    )
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/plans/{plan['id']}/tasks", json=[],
    )
    result = runner.invoke(app, ["plan", "show", plan["id"]])
    assert result.exit_code == 0
    assert "no tasks" in result.output


# ── submit (intent shorthand) ────────────────────────────────────────────────


def test_submit_creates_plan_and_task(httpx_mock: HTTPXMock) -> None:
    plan = _plan_payload()
    task = _task_payload(plan_id=plan["id"])
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json=plan, status_code=201,
    )
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/tasks",
        json=task, status_code=201,
    )
    result = runner.invoke(app, ["submit", "fix the redirect", "-r", "test/repo"])
    assert result.exit_code == 0, result.output
    assert plan["id"] in result.output
    assert task["id"] in result.output


# ── task show / list ─────────────────────────────────────────────────────────


def test_task_show_prints_task(httpx_mock: HTTPXMock) -> None:
    task = _task_payload(task_id="bbbbbbbb-0000-0000-0000-000000000000")
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/tasks/{task['id']}", json=task,
    )
    result = runner.invoke(app, ["task", "show", task["id"]])
    assert result.exit_code == 0
    assert task["id"] in result.output
    assert "registered" in result.output


def test_task_list_prints_table(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url="http://fake-api/api/v1/tasks",
        json=[_task_payload(), _task_payload()],
    )
    result = runner.invoke(app, ["task", "list"])
    assert result.exit_code == 0
    assert "Tasks (2)" in result.output


def test_task_list_handles_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url="http://fake-api/api/v1/tasks", json=[],
    )
    result = runner.invoke(app, ["task", "list"])
    assert result.exit_code == 0
    assert "no tasks match" in result.output


def test_task_list_passes_filters(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url="http://fake-api/api/v1/tasks?repo=foo/bar&derived_status=registered",
        json=[],
    )
    result = runner.invoke(app, [
        "task", "list", "-r", "foo/bar", "--status", "registered",
    ])
    assert result.exit_code == 0


# ── workflows seed-starters ──────────────────────────────────────────────────


def test_workflows_seed_starters_invokes_seed(httpx_mock: HTTPXMock) -> None:
    """Smoke: the command runs ``seed`` against a fake API. Each POST
    (role, workflow, version, event-trigger) returns 201 — first-time install path."""
    from treadmill_api.starters import STARTERS, _DEFAULT_EVENT_TRIGGERS, _all_roles

    # Every role POST returns 201.
    for _ in _all_roles():
        httpx_mock.add_response(
            method="POST", url="http://fake-api/api/v1/roles",
            json={"id": "role-x"}, status_code=201,
        )
    # Every workflow POST + GET + version POST returns 201 / 200.
    for wf in STARTERS:
        httpx_mock.add_response(
            method="POST", url="http://fake-api/api/v1/workflows",
            json={"id": wf["id"]}, status_code=201,
        )
        httpx_mock.add_response(
            method="GET",
            url=f"http://fake-api/api/v1/workflows/{wf['id']}",
            json={"id": wf["id"], "latest_version": None},
        )
        httpx_mock.add_response(
            method="POST",
            url=f"http://fake-api/api/v1/workflows/{wf['id']}/versions",
            json={"id": "v1"}, status_code=201,
        )
    # Every default event-trigger POST returns 201.
    for _ in _DEFAULT_EVENT_TRIGGERS:
        httpx_mock.add_response(
            method="POST", url="http://fake-api/api/v1/event-triggers",
            json={"id": "et-x"}, status_code=201,
        )

    result = runner.invoke(app, ["workflows", "seed-starters"])
    assert result.exit_code == 0, result.output
    assert "seeded" in result.output
    # All seven created on a fresh install.
    assert f"{len(STARTERS)} new of {len(STARTERS)}" in result.output


def test_workflows_seed_starters_idempotent_on_409(httpx_mock: HTTPXMock) -> None:
    """Every POST returns 409 (already-seeded). The GETs report a
    latest_version, so no fresh version POSTs fire. Command exits 0 with
    a ``0 new`` message."""
    from treadmill_api.starters import STARTERS, _DEFAULT_EVENT_TRIGGERS, _all_roles

    for _ in _all_roles():
        httpx_mock.add_response(
            method="POST", url="http://fake-api/api/v1/roles",
            json={"detail": "exists"}, status_code=409,
        )
    for wf in STARTERS:
        httpx_mock.add_response(
            method="POST", url="http://fake-api/api/v1/workflows",
            json={"detail": "exists"}, status_code=409,
        )
        httpx_mock.add_response(
            method="GET",
            url=f"http://fake-api/api/v1/workflows/{wf['id']}",
            json={"id": wf["id"], "latest_version": 1},
        )
        # No version POST expected — GET says latest_version=1 already.
    # Default event-trigger POSTs also already exist.
    for _ in _DEFAULT_EVENT_TRIGGERS:
        httpx_mock.add_response(
            method="POST", url="http://fake-api/api/v1/event-triggers",
            json={"detail": "exists"}, status_code=409,
        )

    result = runner.invoke(app, ["workflows", "seed-starters"])
    assert result.exit_code == 0, result.output
    assert "0 new of" in result.output


# ── status ───────────────────────────────────────────────────────────────────


def test_status_prints_health_and_ready(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url="http://fake-api/health",
        json={"status": "ok", "service": "treadmill-api", "version": "0.0.0"},
    )
    httpx_mock.add_response(
        method="GET", url="http://fake-api/health/ready",
        json={
            "status": "ok",
            "checks": {
                "postgres": {"status": "ok"},
                "redis": {"status": "ok"},
            },
        },
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "liveness" in result.output
    assert "postgres" in result.output


def test_status_reports_unreachable_dep(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url="http://fake-api/health",
        json={"status": "ok", "service": "treadmill-api", "version": "0.0.0"},
    )
    httpx_mock.add_response(
        method="GET", url="http://fake-api/health/ready",
        json={
            "status": "unreachable",
            "checks": {
                "postgres": {"status": "unreachable", "detail": "boom"},
                "redis": {"status": "ok"},
            },
        },
        status_code=503,
    )
    result = runner.invoke(app, ["status"])
    # status_code 503 gets surfaced as ApiError → exit 2.
    assert result.exit_code == 2
