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


# ── ``reason`` filter (ADR-0058 Step 5) ───────────────────────────────────────


def _multi_reason_escalation_session() -> tuple[_StubSession, dict[str, str]]:
    """Three escalated tasks — one per ADR-0058 reason value. Returns the
    session and a ``{reason: task_id}`` map so tests can pin the
    expected surviving row by id without hard-coding uuids."""
    cap_task = _task_row(
        derived_status="wf-architecture-resolve: executing",
        title="Architect cap hit",
        run_id=uuid.uuid4(),
    )
    sweep_task = _task_row(
        derived_status="wf-quick: executing",
        title="Stuck task sweep escalation",
        run_id=uuid.uuid4(),
    )
    gate_task = _task_row(
        derived_status="wf-architecture-resolve: executing",
        title="Gate-broken escalation",
        run_id=uuid.uuid4(),
    )
    escalated_at = _now() - timedelta(minutes=5)
    session = _StubSession(
        tasks=[cap_task, sweep_task, gate_task],
        escalations=[
            {
                "task_id": cap_task["id"], "repo": cap_task["repo"],
                "title": cap_task["title"], "escalated_at": escalated_at,
                "reason": "architect_cap",
            },
            {
                "task_id": sweep_task["id"], "repo": sweep_task["repo"],
                "title": sweep_task["title"], "escalated_at": escalated_at,
                "reason": "stuck_task_sweep",
            },
            {
                "task_id": gate_task["id"], "repo": gate_task["repo"],
                "title": gate_task["title"], "escalated_at": escalated_at,
                "reason": "gate-broken",
            },
        ],
    )
    return session, {
        "architect_cap": cap_task["id"],
        "stuck_task_sweep": sweep_task["id"],
        "gate-broken": gate_task["id"],
    }


@pytest.mark.parametrize(
    "reason", ["architect_cap", "stuck_task_sweep", "gate-broken"],
)
def test_overview_filter_by_reason_narrows_escalations(reason: str) -> None:
    """``?reason=`` narrows ``escalations`` to rows whose escalation
    event's ``payload.reason`` matches. The ADR-0058 sub-classifier
    surface for the dashboard's per-reason badges."""
    session, task_ids = _multi_reason_escalation_session()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/overview", params={"reason": reason},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["escalations"]) == 1
    surfaced = body["escalations"][0]
    assert surfaced["reason"] == reason
    assert surfaced["task_id"] == task_ids[reason]


def test_overview_reason_filter_keeps_bucket_counts_global() -> None:
    """``?reason=`` only narrows the ``escalations`` array — bucket
    counts and the ``tasks`` list stay unfiltered so the page chrome
    keeps reflecting global state (mirrors ``repo``/``account``/``bucket``
    semantics)."""
    session, _ = _multi_reason_escalation_session()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/overview", params={"reason": "gate-broken"},
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["escalations"]) == 1
    # All three tasks remain — every escalation flags its task as
    # ``escalated``, so all three bucket as ``blocked`` regardless of
    # the surfaced-escalations filter.
    assert len(body["tasks"]) == 3
    assert body["bucketCounts"] == {
        "blocked": 3, "inflight": 0, "hopper": 0, "total": 3,
    }
    assert all(t["escalated"] is True for t in body["tasks"])


def test_overview_reason_filter_rejects_unknown_value() -> None:
    """``reason`` is a closed enum — anything outside the three ADR-0058
    values is a 422 from FastAPI's Literal validation."""
    session, _ = _multi_reason_escalation_session()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/overview", params={"reason": "bogus"},
        )
    assert response.status_code == 422


def test_overview_reason_filter_unset_returns_all_escalations() -> None:
    """No ``reason`` param ⇒ all escalations surface (pre-ADR-0058
    behavior is preserved exactly when the filter is omitted)."""
    session, _ = _multi_reason_escalation_session()
    app = _build_app(session)
    with TestClient(app) as client:
        response = client.get("/api/v1/dashboard/overview")
    assert response.status_code == 200
    body = response.json()
    assert {e["reason"] for e in body["escalations"]} == {
        "architect_cap", "stuck_task_sweep", "gate-broken",
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


def test_terminal_filter_uses_pr_merged_not_merged() -> None:
    """The merged-PR projection emits ``pr_merged``, not ``merged``.

    Regression for the 2026-05-28 incident where the Overview returned
    all 86 ``pr_merged`` rows because ``_TASKS_SQL`` was filtering on
    ``'merged'`` (which never matches). Pins both the constant and the
    SQL text so a future tidy that splits one without the other can't
    re-introduce the bug.
    """
    from treadmill_api.routers.dashboard.overview import (
        _TASKS_SQL,
        _TERMINAL_STATUSES,
    )

    assert "pr_merged" in _TERMINAL_STATUSES
    assert "merged" not in _TERMINAL_STATUSES
    # Every terminal status the Python side claims must appear in the
    # SQL filter — otherwise the API returns rows the Python expects
    # to have been excluded.
    for status in _TERMINAL_STATUSES:
        assert f"'{status}'" in _TASKS_SQL, (
            f"_TERMINAL_STATUSES has '{status}' but _TASKS_SQL does not "
            f"reference it; the SQL filter and the Python constant must "
            f"agree."
        )
    # The hybrid ``"pr_merged (wf-author: failed)"`` shape needs the
    # LIKE pattern (a workflow run terminated AFTER the PR auto-merged
    # — the PR is in main, the operator has nothing to do about the
    # run's outcome).
    assert "LIKE 'pr_merged %'" in _TASKS_SQL
