"""Unit tests for ``/api/v1/llm_calls`` (ADR-0087 PR-C + ADR-0089 §2).

All tests use an in-memory stub session — no live Postgres required.

Coverage axes
=============

POST /api/v1/llm_calls
  - creates a row, returns 201 + body with all token fields
  - nullable cache token fields default to None
  - 404 on unknown task_execution_id
  - 400 on negative token counts (Pydantic ge=0 constraint)

GET /api/v1/llm_calls/harvest_cursors
  - returns per-transcript cursor rows

POST /api/v1/llm_calls/harvest (ADR-0089 harvester ingest)
  - inserts calls + upserts the cursor; reports inserted/duplicates
  - duplicate (transcript_path, request_id) pairs counted, not errored
  - 404 on unknown task_execution_id in the batch
  - zero-call batch (malformed-lines-only) still advances the cursor

GET /api/v1/llm_calls/report
  - per-label aggregates + cache-hit ratio + malformed-line total
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import LLMCall, TaskExecution
from treadmill_api.routers.llm_calls import router


# ── Stub helpers ────────────────────────────────────────────────────────


def _stub_execution(execution_id: uuid.UUID) -> MagicMock:
    ex = MagicMock(spec=TaskExecution)
    ex.id = execution_id
    return ex


class _StubSession:
    def __init__(self, *, get_returns: object = None) -> None:
        self._get_returns = get_returns
        self.added: list[object] = []

    async def get(self, model_class, pk):  # noqa: ANN001
        return self._get_returns

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if not hasattr(obj, "id") or obj.id is None:
                object.__setattr__(obj, "id", uuid.uuid4())
            if not hasattr(obj, "created_at") or obj.created_at is None:
                object.__setattr__(obj, "created_at", datetime.now(timezone.utc))

    async def refresh(self, obj: object) -> None:
        pass

    async def commit(self) -> None:
        pass


def _app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    return app


# ── Tests ────────────────────────────────────────────────────────────────


class TestCreateLLMCall:
    def test_creates_row_with_all_fields(self) -> None:
        ex_id = uuid.uuid4()
        session = _StubSession(get_returns=_stub_execution(ex_id))
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/llm_calls",
            json={
                "task_execution_id": str(ex_id),
                "input_tokens": 1500,
                "output_tokens": 800,
                "cache_creation_tokens": 200,
                "cache_read_tokens": 50,
                "model": "claude-sonnet-4-6",
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["task_execution_id"] == str(ex_id)
        assert body["input_tokens"] == 1500
        assert body["output_tokens"] == 800
        assert body["cache_creation_tokens"] == 200
        assert body["cache_read_tokens"] == 50
        assert body["model"] == "claude-sonnet-4-6"
        assert "id" in body
        assert "created_at" in body
        assert len(session.added) == 1

    def test_cache_tokens_optional(self) -> None:
        ex_id = uuid.uuid4()
        session = _StubSession(get_returns=_stub_execution(ex_id))
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/llm_calls",
            json={
                "task_execution_id": str(ex_id),
                "input_tokens": 100,
                "output_tokens": 50,
                "model": "claude-haiku-4-5-20251001",
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["cache_creation_tokens"] is None
        assert body["cache_read_tokens"] is None

    def test_404_unknown_task_execution_id(self) -> None:
        session = _StubSession(get_returns=None)
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/llm_calls",
            json={
                "task_execution_id": str(uuid.uuid4()),
                "input_tokens": 100,
                "output_tokens": 50,
                "model": "claude-sonnet-4-6",
            },
        )
        assert resp.status_code == 404

    def test_negative_tokens_rejected(self) -> None:
        ex_id = uuid.uuid4()
        session = _StubSession(get_returns=_stub_execution(ex_id))
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/llm_calls",
            json={
                "task_execution_id": str(ex_id),
                "input_tokens": -1,
                "output_tokens": 50,
                "model": "claude-sonnet-4-6",
            },
        )
        assert resp.status_code == 422

    def test_zero_tokens_accepted(self) -> None:
        ex_id = uuid.uuid4()
        session = _StubSession(get_returns=_stub_execution(ex_id))
        client = TestClient(_app(session))

        resp = client.post(
            "/api/v1/llm_calls",
            json={
                "task_execution_id": str(ex_id),
                "input_tokens": 0,
                "output_tokens": 0,
                "model": "claude-sonnet-4-6",
            },
        )
        assert resp.status_code == 201, resp.text


# ── ADR-0089 harvest surface ─────────────────────────────────────────────


class _HarvestStubSession:
    """Stub that dispatches ``execute()`` by statement shape.

    The harvest/report endpoints use core statements rather than the
    ORM unit-of-work, so the stub recognises: the execution-id
    existence check, the llm_calls bulk INSERT (returning inserted
    ids), the cursor UPSERT, and the two report SELECTs.
    """

    def __init__(
        self,
        *,
        known_execution_ids: list[uuid.UUID] | None = None,
        insert_outcomes: list[bool] | None = None,
        cursor_rows: list[object] | None = None,
        report_rows: list[object] | None = None,
        malformed_total: int = 0,
    ) -> None:
        self._known_execution_ids = known_execution_ids or []
        # One bool per posted call: the harvest INSERT's RETURNING
        # ``(xmax = 0)`` — True = fresh insert, False = conflict-update.
        self._insert_outcomes = insert_outcomes or []
        self._cursor_rows = cursor_rows or []
        self._report_rows = report_rows or []
        self._malformed_total = malformed_total
        self.call_inserts: list[Insert] = []
        self.cursor_upserts: list[Insert] = []
        self.committed = False

    async def execute(self, stmt):  # noqa: ANN001
        result = MagicMock()
        if isinstance(stmt, Insert):
            if stmt.table.name == "llm_calls":
                self.call_inserts.append(stmt)
                result.scalars.return_value.all.return_value = self._insert_outcomes
            else:
                assert stmt.table.name == "llm_harvest_cursors"
                self.cursor_upserts.append(stmt)
            return result
        sql = str(stmt)
        if "FROM task_executions" in sql:
            result.scalars.return_value.all.return_value = self._known_execution_ids
        elif "FROM llm_harvest_cursors" in sql and "sum(" in sql:
            result.scalar_one.return_value = self._malformed_total
        elif "FROM llm_harvest_cursors" in sql:
            result.scalars.return_value.all.return_value = self._cursor_rows
        else:
            assert "FROM llm_calls" in sql
            result.all.return_value = self._report_rows
        return result

    async def commit(self) -> None:
        self.committed = True


def _harvest_app(session: _HarvestStubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _harvest_call(**overrides: object) -> dict:
    base: dict = {
        "request_id": f"req_{uuid.uuid4().hex[:8]}",
        "session_label": "worker-team1-1",
        "task_execution_id": None,
        "called_at": "2026-06-10T12:00:00+00:00",
        "model": "claude-fable-5",
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_creation_tokens": 1000,
        "cache_read_tokens": 50000,
    }
    base.update(overrides)
    return base


class TestHarvestCursors:
    def test_returns_cursor_rows(self) -> None:
        rows = [
            SimpleNamespace(
                transcript_path="/x/a.jsonl", byte_offset=123, malformed_lines=2
            )
        ]
        session = _HarvestStubSession(cursor_rows=rows)
        client = TestClient(_harvest_app(session))

        resp = client.get("/api/v1/llm_calls/harvest_cursors")

        assert resp.status_code == 200, resp.text
        assert resp.json() == [
            {"transcript_path": "/x/a.jsonl", "byte_offset": 123, "malformed_lines": 2}
        ]


class TestHarvestIngest:
    def test_inserts_calls_and_upserts_cursor(self) -> None:
        ex_id = uuid.uuid4()
        session = _HarvestStubSession(
            known_execution_ids=[ex_id],
            insert_outcomes=[True, True],
        )
        client = TestClient(_harvest_app(session))

        resp = client.post(
            "/api/v1/llm_calls/harvest",
            json={
                "transcript_path": "/x/a.jsonl",
                "byte_offset": 4096,
                "malformed_lines": 1,
                "calls": [
                    _harvest_call(task_execution_id=str(ex_id)),
                    _harvest_call(),
                ],
            },
        )

        assert resp.status_code == 201, resp.text
        assert resp.json() == {"inserted": 2, "updated": 0, "byte_offset": 4096}
        assert len(session.call_inserts) == 1
        assert len(session.cursor_upserts) == 1
        assert session.committed

    def test_straddled_resend_updates_in_place(self) -> None:
        """ON CONFLICT DO UPDATE (last-write-wins): a re-sent straddled
        requestId reports as updated, not inserted, and the statement
        overwrites the usage columns with the excluded (re-sent) values
        rather than dropping the correction (peer-review item 2)."""
        session = _HarvestStubSession(insert_outcomes=[True, False])
        client = TestClient(_harvest_app(session))

        resp = client.post(
            "/api/v1/llm_calls/harvest",
            json={
                "transcript_path": "/x/a.jsonl",
                "byte_offset": 100,
                "calls": [_harvest_call(), _harvest_call()],
            },
        )

        assert resp.status_code == 201, resp.text
        assert resp.json() == {"inserted": 1, "updated": 1, "byte_offset": 100}
        sql = str(
            session.call_inserts[0].compile(dialect=postgresql.dialect())
        )
        assert "ON CONFLICT" in sql and "DO UPDATE" in sql
        assert "output_tokens = excluded.output_tokens" in sql
        assert "(xmax = 0)" in sql

    def test_404_unknown_execution_in_batch(self) -> None:
        session = _HarvestStubSession(known_execution_ids=[])
        client = TestClient(_harvest_app(session))

        resp = client.post(
            "/api/v1/llm_calls/harvest",
            json={
                "transcript_path": "/x/a.jsonl",
                "byte_offset": 100,
                "calls": [_harvest_call(task_execution_id=str(uuid.uuid4()))],
            },
        )

        assert resp.status_code == 404
        assert not session.cursor_upserts

    def test_zero_call_batch_still_advances_cursor(self) -> None:
        """A malformed-lines-only span must still move the byte cursor."""
        session = _HarvestStubSession()
        client = TestClient(_harvest_app(session))

        resp = client.post(
            "/api/v1/llm_calls/harvest",
            json={
                "transcript_path": "/x/a.jsonl",
                "byte_offset": 2048,
                "malformed_lines": 3,
                "calls": [],
            },
        )

        assert resp.status_code == 201, resp.text
        assert resp.json() == {"inserted": 0, "updated": 0, "byte_offset": 2048}
        assert not session.call_inserts
        assert len(session.cursor_upserts) == 1
        # Retry-idempotent: the cursor upsert OVERWRITES the cumulative
        # malformed count; a `+ excluded` add would inflate on retries
        # (peer-review item 3).
        sql = str(
            session.cursor_upserts[0].compile(dialect=postgresql.dialect())
        )
        assert "malformed_lines = excluded.malformed_lines" in sql
        assert "+ excluded.malformed_lines" not in sql


class TestTokenReport:
    def test_per_label_rollup_with_hit_ratio(self) -> None:
        rows = [
            SimpleNamespace(
                session_label="worker-team1-1",
                calls=10,
                input_tokens=100,
                output_tokens=500,
                cache_creation_tokens=900,
                cache_read_tokens=9000,
            )
        ]
        session = _HarvestStubSession(report_rows=rows, malformed_total=7)
        client = TestClient(_harvest_app(session))

        resp = client.get(
            "/api/v1/llm_calls/report", params={"since": "2026-06-10T00:00:00+00:00"}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["malformed_lines_total"] == 7
        (row,) = body["rows"]
        assert row["session_label"] == "worker-team1-1"
        assert row["calls"] == 10
        # 9000 reads / (100 fresh + 900 creation + 9000 reads) = 0.9
        assert row["cache_hit_ratio"] == pytest.approx(0.9)

    def test_zero_denominator_hit_ratio_is_zero(self) -> None:
        rows = [
            SimpleNamespace(
                session_label="worker-team1-1",
                calls=1,
                input_tokens=0,
                output_tokens=5,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            )
        ]
        session = _HarvestStubSession(report_rows=rows)
        client = TestClient(_harvest_app(session))

        resp = client.get(
            "/api/v1/llm_calls/report", params={"since": "2026-06-10T00:00:00+00:00"}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["rows"][0]["cache_hit_ratio"] == 0.0

    def test_missing_since_rejected(self) -> None:
        session = _HarvestStubSession()
        client = TestClient(_harvest_app(session))

        resp = client.get("/api/v1/llm_calls/report")

        assert resp.status_code == 422
