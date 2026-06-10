"""Unit tests for ``/api/v1/team_configs`` + ``/api/v1/queue_depth``
(Task C of the combined ADR-0085+0086 plan).

The router goes through ``TeamConfigStore`` for the CRUD paths and
issues a raw ``text(...)`` query for ``queue_depth``. The stub session
below mocks both seams without touching Postgres so this suite runs in
the default unit pass; an integration test against a real DB lives
separately under ``TREADMILL_INTEGRATION=1``.

Coverage axes:
  * upsert creates → get_by_repo returns the row
  * upsert again with a different label overwrites
  * list_all returns all rows (ordered by repo)
  * delete removes the row + a follow-up GET 404s
  * 404 on get for an unknown repo + 404 on delete for the same
  * queue_depth excludes tasks where created_by matches a registered
    coordinator_label, includes everything else
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.models.team_config import TeamConfig
from treadmill_api.routers.team_configs import router as team_configs_router


# ── Stub data model ─────────────────────────────────────────────────────


class _StubTeamConfig:
    """Attribute-only stand-in for a TeamConfig row.

    The router converts via ``model_validate(row, from_attributes=True)``
    so it only needs attribute access; constructing the real
    SQLAlchemy-mapped class hits descriptor-setattr machinery that's
    awkward to bypass for a stub.
    """

    def __init__(
        self,
        *,
        repo: str,
        coordinator_label: str,
        worker_labels: list[str],
        evaluator_label: str | None = None,
        config_id: uuid.UUID | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.id = config_id or uuid.uuid4()
        self.repo = repo
        self.coordinator_label = coordinator_label
        self.evaluator_label = evaluator_label
        self.worker_labels = list(worker_labels)
        self.created_at = created_at or now
        self.updated_at = updated_at or now


def _make_team_config(
    repo: str,
    coordinator_label: str,
    worker_labels: list[str],
    evaluator_label: str | None = None,
) -> _StubTeamConfig:
    return _StubTeamConfig(
        repo=repo,
        coordinator_label=coordinator_label,
        worker_labels=worker_labels,
        evaluator_label=evaluator_label,
    )


class _QueueDepthRow:
    """Result row stand-in for the queue_depth SQL."""

    def __init__(self, visible: int, in_flight: int) -> None:
        self.visible = visible
        self.in_flight = in_flight


# ── Stub session ────────────────────────────────────────────────────────


class _StubSession:
    """In-memory stand-in for AsyncSession sufficient for the router."""

    def __init__(self) -> None:
        self._team_configs: dict[str, _StubTeamConfig] = {}
        self._tasks: list[dict[str, Any]] = []
        self.commit_calls = 0

    # ── Test seeding helpers ────────────────────────────────────────
    def seed_team_config(
        self,
        repo: str,
        coordinator_label: str,
        worker_labels: list[str],
        evaluator_label: str | None = None,
    ) -> _StubTeamConfig:
        row = _make_team_config(
            repo, coordinator_label, worker_labels, evaluator_label
        )
        self._team_configs[repo] = row
        return row

    def seed_task(self, *, derived_status: str, created_by: str | None) -> None:
        self._tasks.append(
            {"derived_status": derived_status, "created_by": created_by or ""}
        )

    # ── AsyncSession surface used by the store + the queue_depth SQL ─
    async def scalar(self, stmt: Any) -> _StubTeamConfig | None:
        repo = self._extract_repo(stmt)
        if repo is None:
            return None
        return self._team_configs.get(repo)

    async def scalars(self, stmt: Any):
        rows = sorted(self._team_configs.values(), key=lambda r: r.repo)

        class _Iter:
            def __init__(self, items): self._items = items
            def __iter__(self): return iter(self._items)

        return _Iter(rows)

    async def execute(self, stmt: Any):
        compiled_sql = str(stmt)

        # Raw SQL — queue_depth.
        if "task_status" in compiled_sql:
            return _QueueDepthResult(self._compute_queue_depth())

        # ADR-0087 scale-down guard probe — table absent in stub.
        # The router uses ``to_regclass('task_executions')`` to detect
        # whether the table exists; in the stub world it doesn't, so
        # return ``None`` and the guard's in-flight check short-circuits.
        if "to_regclass" in compiled_sql:
            return _ScalarResult(None)
        if "FROM task_executions" in compiled_sql:
            # Defensive — should never reach here in the unit-stub
            # path because the to_regclass branch above short-circuits
            # first, but in case a test mocks evaluations differently.
            return _FetchAllResult([])

        # pg_insert(TeamConfig).on_conflict_do_update — upsert path.
        if "INSERT INTO team_configs" in compiled_sql:
            params = self._params_dict(stmt)
            self._team_configs[params["repo"]] = _make_team_config(
                params["repo"],
                params["coordinator_label"],
                list(params["worker_labels"]),
                evaluator_label=params.get("evaluator_label"),
            )
            return _ExecResult(rowcount=1)

        # DELETE FROM team_configs WHERE repo = :repo.
        if "DELETE FROM team_configs" in compiled_sql:
            repo = self._extract_repo(stmt)
            if repo is not None and repo in self._team_configs:
                del self._team_configs[repo]
                return _ExecResult(rowcount=1)
            return _ExecResult(rowcount=0)

        raise AssertionError(f"unexpected execute() statement: {compiled_sql}")

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        pass

    # ── Helpers ─────────────────────────────────────────────────────
    def _compute_queue_depth(self) -> _QueueDepthRow:
        coordinators = {r.coordinator_label for r in self._team_configs.values()}
        visible = 0
        in_flight = 0
        for t in self._tasks:
            if t["created_by"] in coordinators:
                continue
            if t["derived_status"] == "registered":
                visible += 1
            elif t["derived_status"].endswith(": executing"):
                in_flight += 1
        return _QueueDepthRow(visible, in_flight)

    def _extract_repo(self, stmt: Any) -> str | None:
        try:
            compiled = stmt.compile(compile_kwargs={"literal_binds": False})
            for k, v in compiled.params.items():
                if isinstance(v, str) and ("repo" in k.lower() or len(compiled.params) == 1):
                    return v
        except Exception:
            pass
        return None

    def _params_dict(self, stmt: Any) -> dict[str, Any]:
        compiled = stmt.compile(compile_kwargs={"literal_binds": False})
        return dict(compiled.params)


class _ExecResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _QueueDepthResult:
    def __init__(self, row: _QueueDepthRow) -> None:
        self._row = row

    def one(self) -> _QueueDepthRow:
        return self._row


class _ScalarResult:
    """Wraps ``scalar_one_or_none`` for raw-SQL queries in the stub session."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FetchAllResult:
    """Wraps ``fetchall`` for raw-SQL queries in the stub session."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows


# ── App fixture ─────────────────────────────────────────────────────────


@pytest.fixture
def app_and_session() -> tuple[FastAPI, _StubSession]:
    session = _StubSession()
    app = FastAPI()
    app.include_router(team_configs_router)

    async def _override() -> _StubSession:
        return session

    app.dependency_overrides[get_session] = _override
    return app, session


# ── CRUD tests ──────────────────────────────────────────────────────────


def test_upsert_creates_row_then_get_returns_it(app_and_session) -> None:
    app, session = app_and_session
    client = TestClient(app)

    payload = {
        "repo": "joeLepper/treadmill",
        "coordinator_label": "coordinator-treadmill",
        "worker_labels": ["treadmill-alan", "treadmill-bert"],
    }
    resp = client.post("/api/v1/team_configs", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["repo"] == payload["repo"]
    assert body["coordinator_label"] == payload["coordinator_label"]
    assert body["worker_labels"] == payload["worker_labels"]

    get = client.get(f"/api/v1/team_configs/{payload['repo']}")
    assert get.status_code == 200
    assert get.json()["coordinator_label"] == payload["coordinator_label"]


def test_upsert_overwrites_on_repeat(app_and_session) -> None:
    app, session = app_and_session
    client = TestClient(app)

    client.post(
        "/api/v1/team_configs",
        json={
            "repo": "owner/repo",
            "coordinator_label": "coord-a",
            "worker_labels": ["w-a"],
        },
    )
    client.post(
        "/api/v1/team_configs",
        json={
            "repo": "owner/repo",
            "coordinator_label": "coord-b",
            "worker_labels": ["w-b", "w-c"],
        },
    )
    get = client.get("/api/v1/team_configs/owner/repo")
    assert get.status_code == 200
    assert get.json()["coordinator_label"] == "coord-b"
    assert get.json()["worker_labels"] == ["w-b", "w-c"]


def test_list_all_returns_every_row(app_and_session) -> None:
    app, session = app_and_session
    session.seed_team_config("a/x", "coord-a", ["wa"])
    session.seed_team_config("b/y", "coord-b", ["wb"])
    session.seed_team_config("c/z", "coord-c", ["wc"])
    client = TestClient(app)

    resp = client.get("/api/v1/team_configs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    assert [r["repo"] for r in body] == ["a/x", "b/y", "c/z"]


def test_delete_removes_row(app_and_session) -> None:
    app, session = app_and_session
    session.seed_team_config("doomed/repo", "coord-d", [])
    client = TestClient(app)

    resp = client.delete("/api/v1/team_configs/doomed/repo")
    assert resp.status_code == 204

    after = client.get("/api/v1/team_configs/doomed/repo")
    assert after.status_code == 404


def test_get_unknown_repo_404(app_and_session) -> None:
    app, _ = app_and_session
    client = TestClient(app)
    resp = client.get("/api/v1/team_configs/no/such/repo")
    assert resp.status_code == 404


def test_delete_unknown_repo_404(app_and_session) -> None:
    app, _ = app_and_session
    client = TestClient(app)
    resp = client.delete("/api/v1/team_configs/no/such/repo")
    assert resp.status_code == 404


# ── queue_depth tests ──────────────────────────────────────────────────


def test_queue_depth_excludes_coordinator_authored_tasks(app_and_session) -> None:
    """The queue_depth count excludes tasks where ``created_by`` matches
    any registered coordinator_label. Three live tasks: one authored by
    a coordinator (excluded), one registered + non-coordinator (visible),
    one executing + non-coordinator (in_flight)."""
    app, session = app_and_session
    session.seed_team_config(
        repo="owner/repo",
        coordinator_label="coordinator-medicoder",
        worker_labels=["treadmill-carla"],
    )
    # Excluded — created_by matches coordinator_label.
    session.seed_task(
        derived_status="registered",
        created_by="coordinator-medicoder",
    )
    # Counted as visible.
    session.seed_task(derived_status="registered", created_by="treadmill-alan")
    # Counted as in_flight (derived_status of the form "<workflow>: executing").
    session.seed_task(
        derived_status="wf-author: executing",
        created_by="treadmill-bert",
    )

    client = TestClient(app)
    resp = client.get("/api/v1/queue_depth")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"visible": 1, "in_flight": 1}


def test_queue_depth_empty_returns_zeros(app_and_session) -> None:
    app, _ = app_and_session
    client = TestClient(app)
    resp = client.get("/api/v1/queue_depth")
    assert resp.status_code == 200
    assert resp.json() == {"visible": 0, "in_flight": 0}


def test_queue_depth_no_coordinators_counts_everything(app_and_session) -> None:
    """When no team_configs are registered, the excluded-coordinator set
    is empty, so every task counts. Sanity-checks the LEFT JOIN +
    COALESCE shape doesn't accidentally drop tasks."""
    app, session = app_and_session
    session.seed_task(derived_status="registered", created_by="someone")
    session.seed_task(derived_status="registered", created_by=None)
    session.seed_task(
        derived_status="wf-feedback: executing",
        created_by="another",
    )

    client = TestClient(app)
    resp = client.get("/api/v1/queue_depth")
    assert resp.json() == {"visible": 2, "in_flight": 1}


# ── ADR-0087: evaluator_label round-trip + scale-down guard ─────────────


def test_upsert_round_trips_evaluator_label(app_and_session) -> None:
    """ADR-0087 — new ``evaluator_label`` field on team_configs lands
    on the wire and survives round-trip via GET."""
    app, _ = app_and_session
    client = TestClient(app)
    resp = client.post(
        "/api/v1/team_configs",
        json={
            "repo": "joeLepper/treadmill",
            "coordinator_label": "coordinator-joelepper-treadmill",
            "evaluator_label": "evaluator-joelepper-treadmill",
            "worker_labels": [
                "worker-joelepper-treadmill-1",
                "worker-joelepper-treadmill-2",
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["evaluator_label"] == "evaluator-joelepper-treadmill"

    get = client.get("/api/v1/team_configs/joeLepper/treadmill")
    assert get.status_code == 200
    assert get.json()["evaluator_label"] == "evaluator-joelepper-treadmill"


def test_upsert_evaluator_label_optional(app_and_session) -> None:
    """``evaluator_label`` is optional; pre-ADR-0087 callers that omit
    it get a row with ``evaluator_label = None``."""
    app, _ = app_and_session
    client = TestClient(app)
    resp = client.post(
        "/api/v1/team_configs",
        json={
            "repo": "x/y",
            "coordinator_label": "c-xy",
            "worker_labels": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["evaluator_label"] is None


# Scale-down guard tests — patch _in_flight_task_executions_for_labels
# so we control the in-flight return value without standing up a real
# task_executions table. The router queries via the module-level name;
# monkeypatching it is sufficient.


def test_scale_down_aborts_409_when_in_flight_workers_would_be_removed(
    app_and_session, monkeypatch
) -> None:
    """ADR-0087 §Team bootstrap §Scale-down semantics — reducing
    ``worker_labels`` while a to-be-removed worker has a running
    ``task_execution`` returns 409.
    """
    app, session = app_and_session
    session.seed_team_config(
        repo="x/y",
        coordinator_label="c-xy",
        worker_labels=["w-1", "w-2", "w-3"],
    )

    from treadmill_api.routers import team_configs as router_mod

    async def _fake_in_flight(_session, labels):
        # Simulate w-3 currently running a task.
        return [str(uuid.uuid4())] if "w-3" in labels else []

    monkeypatch.setattr(
        router_mod,
        "_in_flight_task_executions_for_labels",
        _fake_in_flight,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/v1/team_configs",
        json={
            "repo": "x/y",
            "coordinator_label": "c-xy",
            "worker_labels": ["w-1", "w-2"],   # drops w-3
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "scale-down would orphan" in detail
    assert "w-3" in detail


def test_scale_down_succeeds_when_no_in_flight_workers_removed(
    app_and_session, monkeypatch
) -> None:
    """No in-flight task_executions on the to-be-removed labels → the
    scale-down passes through. Trigger is the table-absent / quiet
    state."""
    app, session = app_and_session
    session.seed_team_config(
        repo="x/y",
        coordinator_label="c-xy",
        worker_labels=["w-1", "w-2", "w-3"],
    )

    from treadmill_api.routers import team_configs as router_mod

    async def _fake_in_flight(_session, _labels):
        return []  # no in-flight; safe to scale down

    monkeypatch.setattr(
        router_mod,
        "_in_flight_task_executions_for_labels",
        _fake_in_flight,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/v1/team_configs",
        json={
            "repo": "x/y",
            "coordinator_label": "c-xy",
            "worker_labels": ["w-1", "w-2"],
        },
    )
    assert resp.status_code == 200, resp.text


def test_scale_down_force_skips_check(app_and_session, monkeypatch) -> None:
    """``?force=true`` short-circuits the scale-down guard even when
    in-flight workers WOULD be orphaned. Operator's explicit override.
    """
    app, session = app_and_session
    session.seed_team_config(
        repo="x/y",
        coordinator_label="c-xy",
        worker_labels=["w-1", "w-2", "w-3"],
    )

    from treadmill_api.routers import team_configs as router_mod

    in_flight_called = False

    async def _fake_in_flight(_session, _labels):
        nonlocal in_flight_called
        in_flight_called = True
        return [str(uuid.uuid4())]

    monkeypatch.setattr(
        router_mod,
        "_in_flight_task_executions_for_labels",
        _fake_in_flight,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/v1/team_configs?force=true",
        json={
            "repo": "x/y",
            "coordinator_label": "c-xy",
            "worker_labels": ["w-1"],   # drops w-2, w-3
        },
    )
    assert resp.status_code == 200, resp.text
    # Force path must NOT have queried in-flight at all.
    assert in_flight_called is False


def test_scale_up_does_not_invoke_scale_down_check(
    app_and_session, monkeypatch
) -> None:
    """Scale-up (more workers than current) doesn't trip the guard
    even when in-flight is non-empty — the set of REMOVED labels is
    empty, so the in-flight lookup never fires."""
    app, session = app_and_session
    session.seed_team_config(
        repo="x/y",
        coordinator_label="c-xy",
        worker_labels=["w-1"],
    )

    from treadmill_api.routers import team_configs as router_mod

    in_flight_called = False

    async def _fake_in_flight(_session, _labels):
        nonlocal in_flight_called
        in_flight_called = True
        return [str(uuid.uuid4())]  # would 409 if invoked

    monkeypatch.setattr(
        router_mod,
        "_in_flight_task_executions_for_labels",
        _fake_in_flight,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/v1/team_configs",
        json={
            "repo": "x/y",
            "coordinator_label": "c-xy",
            "worker_labels": ["w-1", "w-2", "w-3"],  # adds w-2, w-3
        },
    )
    assert resp.status_code == 200, resp.text
    assert in_flight_called is False  # no removed labels → no lookup
