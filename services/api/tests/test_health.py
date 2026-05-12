"""Unit tests for the healthcheck endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from treadmill_api.app import create_app
from treadmill_api.dependencies import (
    DependencyProbe,
    ProbeResult,
    ProbeStatus,
)


class _StaticProbe:
    """Test double for a DependencyProbe — returns a pre-baked result."""

    def __init__(self, name: str, status: ProbeStatus, detail: str | None = None):
        self.name = name
        self._status = status
        self._detail = detail

    async def check(self) -> ProbeResult:
        return ProbeResult(self.name, self._status, self._detail)


def _client_with_probes(probes: list[DependencyProbe]) -> TestClient:
    """Build a TestClient with the lifespan handler bypassed and probes
    set directly on app.state."""
    app = create_app()
    # Bypass lifespan startup (which would try to wire real engine/redis from
    # env). Setting probes here is enough for the readiness endpoint.
    app.state.probes = probes
    # Also stub the other state fields the lifespan would have set, in case
    # other tests grow to read them.
    app.state.engine = None
    app.state.redis = None
    return TestClient(app)


@pytest.fixture
def empty_client() -> Iterator[TestClient]:
    yield _client_with_probes([])


def test_health_returns_ok(empty_client: TestClient) -> None:
    response = empty_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "treadmill-api"
    assert "version" in body
    assert body["time"].endswith("Z")


def test_ready_with_no_probes_returns_ok_and_200(empty_client: TestClient) -> None:
    response = empty_client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {}


def test_ready_with_all_ok_probes_returns_ok_and_200() -> None:
    client = _client_with_probes([
        _StaticProbe("postgres", ProbeStatus.OK),
        _StaticProbe("redis", ProbeStatus.OK),
    ])
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {
        "postgres": {"status": "ok"},
        "redis": {"status": "ok"},
    }


def test_ready_with_one_unreachable_returns_unreachable_and_503() -> None:
    client = _client_with_probes([
        _StaticProbe("postgres", ProbeStatus.OK),
        _StaticProbe("redis", ProbeStatus.UNREACHABLE, detail="connection refused"),
    ])
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unreachable"
    assert body["checks"]["postgres"] == {"status": "ok"}
    assert body["checks"]["redis"] == {
        "status": "unreachable",
        "detail": "connection refused",
    }


def test_ready_with_not_configured_probes_still_returns_ok_and_200() -> None:
    """A probe in NOT_CONFIGURED state does not flip readiness — production
    deploys wire all deps; in dev some may be unwired."""
    client = _client_with_probes([
        _StaticProbe("postgres", ProbeStatus.OK),
        _StaticProbe("redis", ProbeStatus.NOT_CONFIGURED),
    ])
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["redis"] == {"status": "not_configured"}


def test_ready_includes_consumer_status() -> None:
    """When a ``CoordinationProbe`` is in the probe list, its status shows
    up in the ``checks`` map of ``/health/ready`` under the
    ``coordination_consumer`` key (per the 2026-05-11 closure plan C.6)."""
    client = _client_with_probes([
        _StaticProbe("postgres", ProbeStatus.OK),
        _StaticProbe("redis", ProbeStatus.OK),
        _StaticProbe("coordination_consumer", ProbeStatus.OK),
    ])
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "coordination_consumer" in body["checks"]
    assert body["checks"]["coordination_consumer"] == {"status": "ok"}


def test_ready_returns_503_when_consumer_unreachable() -> None:
    """A dead consumer flips overall readiness to 503 — the API is alive
    but the only writer of step status is gone, so consumers shouldn't
    rely on it for projected state."""
    client = _client_with_probes([
        _StaticProbe("postgres", ProbeStatus.OK),
        _StaticProbe("redis", ProbeStatus.OK),
        _StaticProbe(
            "coordination_consumer",
            ProbeStatus.UNREACHABLE,
            detail="consumer task is not running",
        ),
    ])
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unreachable"
    assert body["checks"]["coordination_consumer"]["status"] == "unreachable"


def test_openapi_docs_advertise_health_endpoints(empty_client: TestClient) -> None:
    response = empty_client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert "/health" in spec["paths"]
    assert "/health/ready" in spec["paths"]
    health_op = spec["paths"]["/health"]["get"]
    assert "health" in health_op.get("tags", [])
