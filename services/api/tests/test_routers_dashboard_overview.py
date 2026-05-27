"""Unit tests for ``GET /api/v1/dashboard/overview`` (ADR-0056, PR-B1).

These exercise the route handler directly with a stub async session —
no live database. The session stub dispatches by SQL substring to a
fixture-driven row list, mirroring the pattern in
``test_onboarding_router.py``. That keeps the test suite hermetic while
still pinning the queries the router actually issues (changing a
query's structure means re-pinning the substring here).

Coverage:

  * Happy path — multiple tasks across buckets; verifies payload shape
    matches ``services/dashboard/src/api/types.ts`` ``useOverview``
    return.
  * ``repo``    filter narrows tasks; bucketCounts stays global.
  * ``bucket``  filter narrows tasks by operator bucket.
  * ``account`` filter narrows tasks by Claude account.
  * ``q``       filter searches across title / id / repo.
  * Empty DB    → empty arrays + zeroed bucket counts.
  * Escalation  — a task with a recent ``escalated_to_operator`` event
    and no acknowledgement appears in ``escalations`` and lands in the
    ``blocked`` bucket regardless of derived_status.
  * Bucket derivation — escalated/blocked/hopper/inflight match
    ``mock.ts`` ``operatorBucket()`` exactly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.dashboard import router as dashboard_router


# ── Stub session machinery ────────────────────────────────────────────────────


class _StubResult:
    """Minimal result wrapper exposing ``.mappings().all()``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_StubResult":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _StubSession:
    """Routes ``session.execute(text(SQL), params)`` to fixtures by SQL
    substring. The router issues 5 queries; we key on a unique fragment
    of each."""

    def __init__(
        self,
        *,
        tasks: list[dict[str, Any]] | None = None,
        pipelines: list[dict[str, Any]] | None = None,
        escalations: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        accounts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.tasks = tasks or []
        self.pipelines = pipelines or []
        self.escalations = escalations or []
        self.events = events or []
        self.accounts = accounts or []
        self.recorded_params: list[dict[str, Any] | None] = []

    async def execute(
        self, statement: Any, params: dict[str, Any] | None = None,
    ) -> _StubResult:
        self.recorded_params.append(params)
        sql = statement.text if hasattr(statement, "text") else str(statement)

        if "FROM tasks t" in sql and "task_status" in sql and "WHERE" in sql:
            return _StubResult(self.tasks)
        if "FROM workflow_run_steps s" in sql and ":run_ids" in sql:
            return _StubResult(self.pipelines)
        if "last_escalation" in sql:
            return _StubResult(self.escalations)
        if "FROM events e" in sql and "LIMIT :limit" in sql:
            return _StubResult(self.events)
        if "GROUP BY COALESCE(rc.claude_account" in sql:
            return _StubResult(self.accounts)
        raise AssertionError(f"unexpected SQL passed to stub session:\n{sql}")


def _build_app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)

    def _override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    return app


# ── Fixture builders ──────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task_row(
    *,
    derived_status: str,
    repo: str = "osmo/web",
    account: str | None = "osmo",
    title: str = "Sample task",
    run_id: uuid.UUID | None = None,
    workflow_id: str | None = "wf-quick",
    tokens_total: int = 1000,
    pr_number: int | None = None,
    pr_derived_mergeability: str | None = None,
) -> dict[str, Any]:
    now = _now()
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "repo": repo,
        "plan_id": str(uuid.uuid4()),
        "created_at": now - timedelta(hours=1),
        "derived_status": derived_status,
        "repo_mode": "conform",
        "claude_account": account,
        "pr_number": pr_number,
        "pr_branch": "claude/feature" if pr_number else None,
        "pr_head_sha": "deadbee1234" if pr_number else None,
        "pr_ci_conclusion": "success" if pr_number else None,
        "pr_review_decision": "approved" if pr_number else None,
        "pr_validate_decision": "pass" if pr_number else None,
        "pr_conflicting": False if pr_number else None,
        "pr_derived_mergeability": pr_derived_mergeability,
        "latest_run_id": run_id,
        "latest_workflow_id": workflow_id,
        "latest_run_started_at": now - timedelta(minutes=30) if run_id else None,
        "last_activity": now - timedelta(minutes=5),
        "tokens_total": tokens_total,
    }


# ── Happy path ────────────────────────────────────────────────────────────────


def test_overview_happy_path() -> None:
    """Three tasks across the three buckets; events tail + accounts +
    fleet are present. Payload shape matches ``OverviewResponse`` and
    every top-level key the dashboard's ``useOverview`` consumes."""
    inflight_run = uuid.uuid4()
    inflight = _task_row(
        derived_status="wf-quick: executing",
        repo="treadmill/dashboard",
        account="personal",
        title="In-flight task",
        run_id=inflight_run,
    )
    hopper = _task_row(
        derived_status="registered",
        repo="treadmill/core",
        account="personal",
        title="Queued spike",
        run_id=None,
        workflow_id=None,
    )
    blocked = _task_row(
        derived_status="blocked-on-conflict",
        repo="treadmill/core",
        account="personal",
        title="Blocked on conflict",
        run_id=uuid.uuid4(),
        pr_number=312,
        pr_derived_mergeability="blocked-on-conflict",
    )
    session = _StubSession(
        tasks=[inflight, hopper, blocked],
        pipelines=[
            {"run_id": inflight_run, "role": "plan",
             "status": "completed", "step_index": 0},
            {"run_id": inflight_run, "role": "code",
             "status": "running", "step_index": 1},
        ],
        accounts=[
            {"name": "personal", "tokens_24h": 1_842_103},
            {"name": "osmo", "tokens_24h": 5_310_788},
        ],
        events=[
            {
                "id": str(uuid.uuid4()),
                "entity_type": "task",
                "action": "registered",
                "task_id": hopper["id"],
                "repo": "treadmill/core",
                "created_at": _now(),
                "payload": {"detail": "via /author skill"},
            },
        ],
    )
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.get("/api/v1/dashboard/overview")

    assert response.status_code == 200, response.text
    body = response.json()

    # Top-level keys mirror ``useOverview`` 1:1.
    assert set(body) == {
        "accounts", "fleet", "escalations", "tasks", "bucketCounts", "events",
    }
    assert body["bucketCounts"] == {
        "blocked": 1, "inflight": 1, "hopper": 1, "total": 3,
    }

    # Pipeline rolled up from the per-run steps; mock.ts maps
    # 'completed' → 'done'.
    inflight_task = next(t for t in body["tasks"] if t["title"] == "In-flight task")
    assert inflight_task["pipeline"] == [
        {"role": "plan", "status": "done"},
        {"role": "code", "status": "running"},
    ]
    assert inflight_task["workflow"] == "wf-quick"
    assert inflight_task["pr"] is None
    assert inflight_task["escalated"] is False

    # Blocked task carries its PR with merged-shape PR fields.
    blocked_task = next(t for t in body["tasks"] if t["title"] == "Blocked on conflict")
    assert blocked_task["pr"]["pr_number"] == 312
    assert blocked_task["pr"]["derived_mergeability"] == "blocked-on-conflict"
    assert blocked_task["pr"]["head_sha"] == "deadbee"  # short SHA (first 7)

    # Accounts strip carries the rolled-up token totals + USD estimate.
    assert {a["name"] for a in body["accounts"]} == {"personal", "osmo"}
    for acct in body["accounts"]:
        assert acct["usd_est_24h"] > 0

    # Fleet present (stubbed) — operator strip renders honestly.
    assert "workers_running" in body["fleet"]
    assert "scheduler_last_tick" in body["fleet"]


# ── Filters ───────────────────────────────────────────────────────────────────


def _filterable_fixtures() -> _StubSession:
    """Three tasks spread across two repos / two accounts / three buckets,
    used by every filter test."""
    return _StubSession(
        tasks=[
            _task_row(
                derived_status="wf-quick: executing",
                repo="treadmill/dashboard",
                account="personal",
                title="Dashboard perf",
            ),
            _task_row(
                derived_status="registered",
                repo="treadmill/core",
                account="personal",
                title="Hopper task",
            ),
            _task_row(
                derived_status="blocked-on-ci",
                repo="osmo/web",
                account="osmo",
                title="Auth callback async migration",
            ),
        ],
    )


def test_overview_filter_by_repo() -> None:
    session = _filterable_fixtures()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/overview", params={"repo": "osmo/web"},
        )
    assert response.status_code == 200
    body = response.json()
    assert [t["repo"] for t in body["tasks"]] == ["osmo/web"]
    # Bucket counts stay global so the page chrome doesn't lie.
    assert body["bucketCounts"]["total"] == 3


def test_overview_filter_by_bucket_inflight() -> None:
    session = _filterable_fixtures()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/overview", params={"bucket": "inflight"},
        )
    assert response.status_code == 200
    body = response.json()
    assert [t["title"] for t in body["tasks"]] == ["Dashboard perf"]


def test_overview_filter_by_bucket_blocked() -> None:
    session = _filterable_fixtures()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/overview", params={"bucket": "blocked"},
        )
    assert response.status_code == 200
    body = response.json()
    assert [t["title"] for t in body["tasks"]] == [
        "Auth callback async migration",
    ]


def test_overview_filter_by_bucket_hopper() -> None:
    session = _filterable_fixtures()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/overview", params={"bucket": "hopper"},
        )
    assert response.status_code == 200
    body = response.json()
    assert [t["title"] for t in body["tasks"]] == ["Hopper task"]


def test_overview_filter_by_account() -> None:
    session = _filterable_fixtures()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/overview", params={"account": "osmo"},
        )
    assert response.status_code == 200
    body = response.json()
    assert {t["account"] for t in body["tasks"]} == {"osmo"}


def test_overview_filter_by_query_substring() -> None:
    """Case-insensitive substring over title / id / repo."""
    session = _filterable_fixtures()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/overview", params={"q": "AUTH"},
        )
    assert response.status_code == 200
    body = response.json()
    assert [t["title"] for t in body["tasks"]] == [
        "Auth callback async migration",
    ]


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_overview_empty_database_returns_empty_arrays() -> None:
    session = _StubSession()  # all fixtures default to []
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get("/api/v1/dashboard/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tasks"] == []
    assert body["events"] == []
    assert body["escalations"] == []
    assert body["accounts"] == []
    assert body["bucketCounts"] == {
        "blocked": 0, "inflight": 0, "hopper": 0, "total": 0,
    }


def test_overview_escalated_task_lands_in_blocked_bucket() -> None:
    """A task that's neither status-blocked nor hopper, but carries an
    unacknowledged ``escalated_to_operator`` event, must surface in
    ``escalations`` AND get ``blocked`` from operator_bucket() per the
    mock contract."""
    run_id = uuid.uuid4()
    task = _task_row(
        derived_status="wf-feedback: executing",
        title="Escalated mid-flight",
        run_id=run_id,
    )
    session = _StubSession(
        tasks=[task],
        escalations=[
            {
                "task_id": task["id"],
                "repo": task["repo"],
                "title": task["title"],
                "escalated_at": _now() - timedelta(minutes=10),
                "reason": "stuck > 10m on failing CI",
            },
        ],
    )
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get("/api/v1/dashboard/overview")
    assert response.status_code == 200
    body = response.json()
    assert len(body["escalations"]) == 1
    assert body["escalations"][0]["reason"] == "stuck > 10m on failing CI"
    surfaced = body["tasks"][0]
    assert surfaced["escalated"] is True
    assert surfaced["escalation_reason"] == "stuck > 10m on failing CI"
    # Despite the executing status, the operator bucket is `blocked`.
    assert body["bucketCounts"] == {
        "blocked": 1, "inflight": 0, "hopper": 0, "total": 1,
    }


# ── Bucket derivation parity with mock.ts `operatorBucket()` ──────────────────


@pytest.mark.parametrize(
    ("derived_status", "escalated", "expected_bucket"),
    [
        ("wf-quick: executing", False, "inflight"),
        ("awaiting_review", False, "inflight"),
        ("blocked", False, "blocked"),
        ("blocked-on-ci", False, "blocked"),
        ("blocked-on-review", False, "blocked"),
        ("registered", False, "hopper"),
        ("queued", False, "hopper"),
        # Escalation overrides derived_status, per mock.ts.
        ("wf-quick: executing", True, "blocked"),
        ("registered", True, "blocked"),
    ],
)
def test_operator_bucket_parity_with_mock(
    derived_status: str, escalated: bool, expected_bucket: str,
) -> None:
    from treadmill_api.routers.dashboard.overview import operator_bucket

    assert operator_bucket(
        derived_status=derived_status, escalated=escalated,
    ) == expected_bucket
