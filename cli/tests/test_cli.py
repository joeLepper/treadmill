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


# ── plan submit --doc: status auto-promotion ─────────────────────────────────


_DOC_WITH_STATUS = """\
---
status: {status}
---

# Plan

## sequence_of_work

```yaml
sequence_of_work:
  - id: t1
    title: do thing
    workflow: wf-author
    intent: do it
    scope:
      files:
        - src/x.py
    validation:
      - kind: deterministic
        description: passes
        script: exit 0
```
"""


@pytest.mark.parametrize("initial_status,expected_in_body", [
    ("drafting", "status: active"),
    ("active", "status: active"),
])
def test_plan_submit_doc_status_promoted_to_active(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    initial_status: str,
    expected_in_body: str,
) -> None:
    """Doc with status: drafting is auto-promoted to active before submission;
    doc already active passes through unchanged."""
    plan = _plan_payload(doc_path="plan.md")
    plan_id = plan["id"]

    doc_file = tmp_path / "plan.md"
    doc_file.write_text(_DOC_WITH_STATUS.format(status=initial_status))

    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json={**plan, "doc_path": str(doc_file)}, status_code=201,
    )
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/plans/{plan_id}/tasks",
        json=[],
    )

    result = runner.invoke(app, [
        "plan", "submit", "-r", "test/repo", "-d", str(doc_file),
    ])
    assert result.exit_code == 0, result.output

    import json as _json
    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    body = _json.loads(posts[0].content)
    assert expected_in_body in body["doc_content"]


def test_plan_submit_doc_drafting_no_local_write_without_dev(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
) -> None:
    """Without --dev, flipping drafting → active does NOT write the local file
    (the bump travels in the PR; the local copy stays at drafting until merge)."""
    plan = _plan_payload()
    plan_id = plan["id"]

    original = _DOC_WITH_STATUS.format(status="drafting")
    doc_file = tmp_path / "plan.md"
    doc_file.write_text(original)

    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json=plan, status_code=201,
    )
    httpx_mock.add_response(
        method="GET", url=f"http://fake-api/api/v1/plans/{plan_id}/tasks",
        json=[],
    )

    runner.invoke(app, [
        "plan", "submit", "-r", "test/repo", "-d", str(doc_file),
    ])
    # Local file is unchanged — the PR carries the bump.
    assert doc_file.read_text() == original


def test_plan_submit_doc_with_completed_status_refuses(tmp_path: Path) -> None:
    """Submitting a plan doc whose frontmatter status is 'completed' must
    exit with code 2 and a helpful error — the plan is terminal."""
    doc_file = tmp_path / "plan.md"
    doc_file.write_text(_DOC_WITH_STATUS.format(status="completed"))

    result = runner.invoke(app, [
        "plan", "submit", "-r", "test/repo", "-d", str(doc_file),
    ])
    assert result.exit_code == 2
    assert "completed" in result.output


def test_plan_submit_doc_with_abandoned_status_refuses(tmp_path: Path) -> None:
    """Submitting a plan doc whose frontmatter status is 'abandoned' must
    exit with code 2."""
    doc_file = tmp_path / "plan.md"
    doc_file.write_text(_DOC_WITH_STATUS.format(status="abandoned"))

    result = runner.invoke(app, [
        "plan", "submit", "-r", "test/repo", "-d", str(doc_file),
    ])
    assert result.exit_code == 2
    assert "abandoned" in result.output


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


# The two ``workflows seed-starters`` tests were deleted with the command
# itself (ADR-0087 Phase 5 / PR-G removed ``treadmill_api.starters`` and
# the tables it seeded — the tests exercised deleted behavior).


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


# ── role show / update / versions (ADR-0028) ─────────────────────────────────


def _role_payload(role_id: str = "role-reviewer", **overrides) -> dict:
    base = {
        "id": role_id,
        "model": "claude-3.5",
        "system_prompt": "you are a careful reviewer.",
        "output_kind": "review",
        "skills": [],
        "hooks": [],
        "created_at": "2026-05-13T12:00:00Z",
        "updated_at": "2026-05-13T12:00:00Z",
    }
    base.update(overrides)
    return base


def test_role_show_displays_live_prompt(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url="http://fake-api/api/v1/roles/role-reviewer",
        json=_role_payload(),
    )
    result = runner.invoke(app, ["role", "show", "role-reviewer"])
    assert result.exit_code == 0, result.output
    assert "role-reviewer" in result.output
    assert "you are a careful reviewer." in result.output
    assert "review" in result.output  # output_kind


def test_role_show_with_version_displays_snapshot(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://fake-api/api/v1/roles/role-reviewer/versions/2",
        json={
            "version": 2,
            "system_prompt": "version 2 prompt body",
            "notes": "tightened verdict criteria",
            "pr_url": "https://github.com/x/y/pull/42",
            "created_at": "2026-05-13T13:00:00Z",
            "created_by": "api",
        },
    )
    result = runner.invoke(
        app, ["role", "show", "role-reviewer", "--version", "2"],
    )
    assert result.exit_code == 0, result.output
    assert "v2" in result.output
    assert "version 2 prompt body" in result.output
    assert "tightened verdict criteria" in result.output
    assert "https://github.com/x/y/pull/42" in result.output


def test_role_show_handles_404(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url="http://fake-api/api/v1/roles/role-missing",
        json={"detail": "role not found"}, status_code=404,
    )
    result = runner.invoke(app, ["role", "show", "role-missing"])
    assert result.exit_code == 2


def test_role_update_patches_with_file_contents(
    httpx_mock: HTTPXMock, tmp_path: Path,
) -> None:
    prompt_file = tmp_path / "new_prompt.md"
    prompt_file.write_text("you are an even more careful reviewer.\n")

    httpx_mock.add_response(
        method="PATCH", url="http://fake-api/api/v1/roles/role-reviewer",
        json={"role": _role_payload(), "version": 2},
    )

    result = runner.invoke(app, [
        "role", "update", "role-reviewer",
        "--prompt-from-file", str(prompt_file),
    ])
    assert result.exit_code == 0, result.output
    assert "updated" in result.output
    assert "version 2" in result.output

    # Body contains the file's contents as system_prompt.
    patches = [r for r in httpx_mock.get_requests() if r.method == "PATCH"]
    assert len(patches) == 1
    import json
    body = json.loads(patches[0].content)
    assert body["system_prompt"] == "you are an even more careful reviewer.\n"


def test_role_update_propagates_notes_and_pr_url(
    httpx_mock: HTTPXMock, tmp_path: Path,
) -> None:
    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("new prompt body")

    httpx_mock.add_response(
        method="PATCH", url="http://fake-api/api/v1/roles/role-x",
        json={"role": _role_payload(role_id="role-x"), "version": 3},
    )

    result = runner.invoke(app, [
        "role", "update", "role-x",
        "--prompt-from-file", str(prompt_file),
        "--notes", "incident response for o11y smoke",
        "--pr-url", "https://github.com/joeLepper/treadmill/pull/99",
    ])
    assert result.exit_code == 0, result.output

    import json
    patch = next(
        r for r in httpx_mock.get_requests() if r.method == "PATCH"
    )
    body = json.loads(patch.content)
    assert body["notes"] == "incident response for o11y smoke"
    assert body["pr_url"] == "https://github.com/joeLepper/treadmill/pull/99"


def test_role_update_missing_file_errors() -> None:
    result = runner.invoke(app, [
        "role", "update", "role-x",
        "--prompt-from-file", "/nonexistent/path.md",
    ])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_role_update_empty_file_errors(tmp_path: Path) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("   \n  \n")
    result = runner.invoke(app, [
        "role", "update", "role-x", "--prompt-from-file", str(empty),
    ])
    assert result.exit_code == 2
    assert "empty" in result.output


def test_role_versions_lists_history(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url="http://fake-api/api/v1/roles/role-reviewer/versions",
        json=[
            {
                "version": 2, "notes": "edit",
                "pr_url": "https://github.com/x/y/pull/42",
                "created_at": "2026-05-13T13:00:00Z", "created_by": "api",
            },
            {
                "version": 1, "notes": "initial",
                "pr_url": None,
                "created_at": "2026-05-13T12:00:00Z", "created_by": "api",
            },
        ],
    )
    result = runner.invoke(app, ["role", "versions", "role-reviewer"])
    assert result.exit_code == 0, result.output
    assert "2" in result.output and "1" in result.output
    assert "edit" in result.output
    assert "initial" in result.output


def test_role_versions_empty_lists_friendly_message(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="GET", url="http://fake-api/api/v1/roles/role-empty/versions",
        json=[],
    )
    result = runner.invoke(app, ["role", "versions", "role-empty"])
    assert result.exit_code == 0
    assert "no versions" in result.output
