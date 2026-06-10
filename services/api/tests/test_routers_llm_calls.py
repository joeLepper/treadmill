"""Unit tests for ``/api/v1/llm_calls`` (ADR-0087 PR-C).

All tests use an in-memory stub session — no live Postgres required.

Coverage axes
=============

POST /api/v1/llm_calls
  - creates a row, returns 201 + body with all token fields
  - nullable cache token fields default to None
  - 404 on unknown task_execution_id
  - 400 on negative token counts (Pydantic ge=0 constraint)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
