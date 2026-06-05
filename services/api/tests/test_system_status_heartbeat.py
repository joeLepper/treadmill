"""Tests for the system_status heartbeat and detector API.

POST /api/v1/system_status/heartbeat — upsert autoscaler state.
GET  /api/v1/system_status/{family}  — read current state.

Uses AsyncMock for session so tests run without live Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.models.system_status import SystemStatus
from treadmill_api.routers import system_status as router_mod


def _build_app(session_override: AsyncMock) -> FastAPI:
    app = FastAPI()
    app.include_router(router_mod.router)

    async def _session_override() -> AsyncIterator[AsyncMock]:
        yield session_override

    app.dependency_overrides[get_session] = _session_override
    return app


# ── POST /api/v1/system_status/heartbeat ─────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_creates_new_row() -> None:
    """POST heartbeat creates a new row when family doesn't exist."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)  # No existing row
    session.add = MagicMock()
    session.commit = AsyncMock()

    app = _build_app(session)
    client = TestClient(app)

    now = datetime.now(timezone.utc)
    response = client.post(
        "/api/v1/system_status/heartbeat",
        json={
            "family": "worker-default",
            "worker_count": 5,
            "last_spawn_at": now.isoformat(),
            "last_spawn_error": None,
            "consecutive_spawn_failures": 0,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_heartbeat_upserts_existing_row() -> None:
    """POST heartbeat updates an existing row."""
    existing_row = MagicMock(spec=SystemStatus)
    existing_row.family = "worker-default"
    existing_row.worker_count = 3

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=existing_row)
    session.commit = AsyncMock()

    app = _build_app(session)
    client = TestClient(app)

    now = datetime.now(timezone.utc)
    response = client.post(
        "/api/v1/system_status/heartbeat",
        json={
            "family": "worker-default",
            "worker_count": 5,
            "last_spawn_at": now.isoformat(),
            "last_spawn_error": None,
            "consecutive_spawn_failures": 0,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert existing_row.worker_count == 5
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_heartbeat_increments_failure_counter() -> None:
    """POST heartbeat with failure path increments consecutive_spawn_failures."""
    existing_row = MagicMock(spec=SystemStatus)
    existing_row.consecutive_spawn_failures = 2

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=existing_row)
    session.commit = AsyncMock()

    app = _build_app(session)
    client = TestClient(app)

    response = client.post(
        "/api/v1/system_status/heartbeat",
        json={
            "family": "worker-default",
            "worker_count": 5,
            "last_spawn_at": None,
            "last_spawn_error": "docker build failed: ...",
            "consecutive_spawn_failures": 3,
        },
    )

    assert response.status_code == 200
    assert existing_row.consecutive_spawn_failures == 3


# ── GET /api/v1/system_status/{family} ──────────────────────────────────


@pytest.mark.asyncio
async def test_get_reads_existing_row() -> None:
    """GET reads and returns current system status."""
    now = datetime.now(timezone.utc)
    row = MagicMock(spec=SystemStatus)
    row.family = "worker-default"
    row.worker_count = 5
    row.last_spawn_at = now
    row.last_spawn_error = None
    row.last_consume_at = None
    row.consecutive_spawn_failures = 0
    row.updated_at = now

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=row)

    app = _build_app(session)
    client = TestClient(app)

    response = client.get("/api/v1/system_status/worker-default")

    assert response.status_code == 200
    data = response.json()
    assert data["family"] == "worker-default"
    assert data["worker_count"] == 5
    assert data["consecutive_spawn_failures"] == 0


@pytest.mark.asyncio
async def test_get_returns_404_when_family_not_found() -> None:
    """GET returns 404 when family doesn't exist."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)

    app = _build_app(session)
    client = TestClient(app)

    response = client.get("/api/v1/system_status/nonexistent-family")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()
