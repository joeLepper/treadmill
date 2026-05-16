"""Tests for treadmill learnings crystallize command."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from treadmill_cli.cli import app
from treadmill_cli.commands.learnings import _is_candidate, scan_learnings


runner = CliRunner()


@pytest.fixture(autouse=True)
def _api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREADMILL_API_URL", "http://fake-api")


def _write_learning(
    directory: Path,
    slug: str,
    *,
    status: str = "captured",
    backoff_until: str | None = None,
    date_prefix: str = "2026-05-14",
) -> Path:
    lines = ["---", f"date: {date_prefix}", f"status: {status}", "trigger: test"]
    if backoff_until is not None:
        lines.append(f"crystallization_backoff_until: {backoff_until}")
    lines += ["---", "", f"# Learning: {slug}", ""]
    path = directory / f"{date_prefix}-{slug}.md"
    path.write_text("\n".join(lines))
    return path


def _plan_payload(**overrides: object) -> dict:
    base: dict = {
        "id": str(uuid.uuid4()),
        "repo": "test/repo",
        "intent": "x",
        "doc_path": None,
        "parent_plan_id": None,
        "created_by": None,
        "created_at": "2026-05-14T00:00:00Z",
    }
    base.update(overrides)
    return base


def _task_payload(**overrides: object) -> dict:
    base: dict = {
        "id": str(uuid.uuid4()),
        "plan_id": str(uuid.uuid4()),
        "repo": "test/repo",
        "title": "T",
        "description": "desc",
        "workflow_version_id": str(uuid.uuid4()),
        "created_by": None,
        "created_at": "2026-05-14T00:00:00Z",
        "derived_status": "registered",
    }
    base.update(overrides)
    return base


# ── _is_candidate ─────────────────────────────────────────────────────────────


def test_is_candidate_status_captured_is_included(tmp_path: Path) -> None:
    path = _write_learning(tmp_path, "test-learning", status="captured")
    assert _is_candidate(path) is True


def test_is_candidate_status_crystallized_is_excluded(tmp_path: Path) -> None:
    path = _write_learning(tmp_path, "test-learning", status="crystallized-into-rule-foo")
    assert _is_candidate(path) is False


def test_is_candidate_status_open_is_excluded(tmp_path: Path) -> None:
    path = _write_learning(tmp_path, "test-learning", status="open")
    assert _is_candidate(path) is False


def test_is_candidate_active_backoff_is_excluded(tmp_path: Path) -> None:
    future = (date.today() + timedelta(days=7)).isoformat()
    path = _write_learning(tmp_path, "test-learning", status="captured", backoff_until=future)
    assert _is_candidate(path) is False


def test_is_candidate_expired_backoff_is_included(tmp_path: Path) -> None:
    past = (date.today() - timedelta(days=1)).isoformat()
    path = _write_learning(tmp_path, "test-learning", status="captured", backoff_until=past)
    assert _is_candidate(path) is True


def test_is_candidate_backoff_equal_to_today_is_included(tmp_path: Path) -> None:
    today = date.today().isoformat()
    path = _write_learning(tmp_path, "test-learning", status="captured", backoff_until=today)
    assert _is_candidate(path) is True


def test_is_candidate_no_backoff_field_is_included(tmp_path: Path) -> None:
    path = _write_learning(tmp_path, "test-learning", status="captured")
    assert _is_candidate(path) is True


def test_is_candidate_missing_file_returns_false(tmp_path: Path) -> None:
    assert _is_candidate(tmp_path / "nonexistent.md") is False


def test_is_candidate_no_frontmatter_is_excluded(tmp_path: Path) -> None:
    path = tmp_path / "bare.md"
    path.write_text("# No frontmatter here\n")
    assert _is_candidate(path) is False


# ── scan_learnings ────────────────────────────────────────────────────────────


def test_scan_learnings_empty_dir(tmp_path: Path) -> None:
    assert scan_learnings(tmp_path) == []


def test_scan_learnings_missing_dir(tmp_path: Path) -> None:
    assert scan_learnings(tmp_path / "nonexistent") == []


def test_scan_learnings_excludes_crystallized(tmp_path: Path) -> None:
    _write_learning(tmp_path, "captured-one", status="captured")
    _write_learning(tmp_path, "crystallized-one", status="crystallized-into-rule-foo")
    slugs = scan_learnings(tmp_path)
    assert slugs == ["2026-05-14-captured-one"]


def test_scan_learnings_excludes_active_backoff(tmp_path: Path) -> None:
    future = (date.today() + timedelta(days=3)).isoformat()
    _write_learning(tmp_path, "backoff-active", status="captured", backoff_until=future)
    _write_learning(tmp_path, "no-backoff", status="captured")
    slugs = scan_learnings(tmp_path)
    assert slugs == ["2026-05-14-no-backoff"]


def test_scan_learnings_returns_sorted_slugs(tmp_path: Path) -> None:
    (tmp_path / "2026-05-12-alpha.md").write_text("---\nstatus: captured\n---\n")
    (tmp_path / "2026-05-10-beta.md").write_text("---\nstatus: captured\n---\n")
    assert scan_learnings(tmp_path) == ["2026-05-10-beta", "2026-05-12-alpha"]


def test_scan_learnings_mixed_statuses(tmp_path: Path) -> None:
    _write_learning(tmp_path, "captured-a", status="captured", date_prefix="2026-05-10")
    _write_learning(tmp_path, "open-b", status="open", date_prefix="2026-05-11")
    _write_learning(tmp_path, "captured-c", status="captured", date_prefix="2026-05-12")
    slugs = scan_learnings(tmp_path)
    assert slugs == ["2026-05-10-captured-a", "2026-05-12-captured-c"]


# ── crystallize CLI command ───────────────────────────────────────────────────


def test_crystallize_no_candidates_exits_zero(tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "learnings", "crystallize", "-r", "test/repo",
        "--learnings-dir", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    assert "no captured learnings" in result.output


def test_crystallize_dispatches_single_task(
    httpx_mock: HTTPXMock, tmp_path: Path,
) -> None:
    _write_learning(tmp_path, "first-learning", status="captured")
    _write_learning(tmp_path, "second-learning", status="captured")
    _write_learning(tmp_path, "already-done", status="crystallized-into-rule-foo")

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

    result = runner.invoke(app, [
        "learnings", "crystallize", "-r", "test/repo",
        "--learnings-dir", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    assert "dispatched" in result.output
    assert plan["id"] in result.output
    assert task["id"] in result.output
    assert "candidates: 2" in result.output

    # Exactly one task POST.
    task_posts = [
        r for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.path == "/api/v1/tasks"
    ]
    assert len(task_posts) == 1


def test_crystallize_task_payload_carries_candidate_slugs(
    httpx_mock: HTTPXMock, tmp_path: Path,
) -> None:
    _write_learning(tmp_path, "alpha", status="captured", date_prefix="2026-05-10")
    _write_learning(tmp_path, "beta", status="captured", date_prefix="2026-05-11")
    _write_learning(tmp_path, "skipped", status="open", date_prefix="2026-05-12")

    plan = _plan_payload()
    task = _task_payload(plan_id=plan["id"])
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans", json=plan, status_code=201,
    )
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/tasks", json=task, status_code=201,
    )

    runner.invoke(app, [
        "learnings", "crystallize", "-r", "test/repo",
        "--learnings-dir", str(tmp_path),
    ])

    task_post = next(
        r for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.path == "/api/v1/tasks"
    )
    body = json.loads(task_post.content)
    assert body["workflow"] == "wf-crystallize-learning"
    assert "2026-05-10-alpha" in body["description"]
    assert "2026-05-11-beta" in body["description"]
    assert "skipped" not in body["description"]


def test_crystallize_excludes_backoff_active_learnings(
    httpx_mock: HTTPXMock, tmp_path: Path,
) -> None:
    future = (date.today() + timedelta(days=14)).isoformat()
    _write_learning(tmp_path, "backoff-still-active", status="captured", backoff_until=future)
    _write_learning(tmp_path, "ready-to-go", status="captured")

    plan = _plan_payload()
    task = _task_payload(plan_id=plan["id"])
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans", json=plan, status_code=201,
    )
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/tasks", json=task, status_code=201,
    )

    result = runner.invoke(app, [
        "learnings", "crystallize", "-r", "test/repo",
        "--learnings-dir", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    assert "candidates: 1" in result.output

    task_post = next(
        r for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.path == "/api/v1/tasks"
    )
    body = json.loads(task_post.content)
    assert "ready-to-go" in body["description"]
    assert "backoff-still-active" not in body["description"]


def test_crystallize_surfaces_api_error(
    httpx_mock: HTTPXMock, tmp_path: Path,
) -> None:
    _write_learning(tmp_path, "test-learning", status="captured")
    httpx_mock.add_response(
        method="POST", url="http://fake-api/api/v1/plans",
        json={"detail": "forbidden"}, status_code=403,
    )
    result = runner.invoke(app, [
        "learnings", "crystallize", "-r", "test/repo",
        "--learnings-dir", str(tmp_path),
    ])
    assert result.exit_code == 2
    assert "403" in result.output
