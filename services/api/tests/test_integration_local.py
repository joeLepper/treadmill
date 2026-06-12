"""Integration tests against the live API in the spike substrate.

These are the tests that satisfy the Phase 2 Day 1 gate from the plan:
*"a passing test that hits the live API in the spike substrate."* They
exercise the real uvicorn process, the real Postgres + Redis containers,
and the real network wiring — not the FastAPI TestClient.

Skipped by default. To run:

  treadmill-local up      # bring up moto + postgres + redis + api locally
  TREADMILL_INTEGRATION=1 uv run pytest services/api/tests/test_integration_local.py
  treadmill-local down

When ``TREADMILL_API_URL`` is set, the tests target that URL; otherwise
they default to ``http://localhost:8088`` (the local-adapter's host port
mapping for the API service per ADR-0010 and the spike CDK).
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
# Task 3aaba5e7: NO live-API default. TREADMILL_API_URL is ambient in
# every team-session env (pointing at the LIVE deployment) and
# localhost:8088 IS the live stack on the operator host — a dedicated
# test var makes hitting an API an explicit act.
TEST_API_URL = os.environ.get("TREADMILL_TEST_API_URL")
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_API_URL),
    reason=(
        "set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_API_URL (a test "
        "API instance, never the live one) to run; requires a dedicated "
        "`treadmill-local up`-style stack"
    ),
)


@pytest.fixture(scope="module")
def api_url() -> str:
    return TEST_API_URL


@pytest.fixture(scope="module")
def client(api_url: str):
    """Wait for the API to be reachable, then yield an httpx Client.

    The substrate may be in mid-startup when the test starts (Postgres /
    Redis pull or boot races). We give it 30s to settle before failing.
    """
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                break
        except Exception as exc:
            last_error = exc
        time.sleep(0.5)
    else:
        raise RuntimeError(
            f"API at {api_url} did not become reachable within 30s "
            f"(last error: {last_error!r})"
        )
    with httpx.Client(base_url=api_url, timeout=5.0) as c:
        yield c


def test_health_returns_ok(client: httpx.Client) -> None:
    response = client.get("/health")
    response.raise_for_status()
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "treadmill-api"
    assert body["time"].endswith("Z")


def test_ready_reports_postgres_reachable(client: httpx.Client) -> None:
    """Postgres container DNS (``treadmill-postgres:5432``) resolves from
    inside the API container; the readiness probe's ``SELECT 1`` succeeds."""
    response = client.get("/health/ready")
    response.raise_for_status()
    body = response.json()
    assert body["checks"]["postgres"]["status"] == "ok"


def test_ready_reports_redis_reachable(client: httpx.Client) -> None:
    """Redis container DNS (``treadmill-redis:6379``) resolves from inside
    the API container; the readiness probe's ``PING`` succeeds."""
    response = client.get("/health/ready")
    response.raise_for_status()
    body = response.json()
    assert body["checks"]["redis"]["status"] == "ok"


def test_ready_overall_ok_when_all_deps_reachable(client: httpx.Client) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


def test_openapi_is_served(client: httpx.Client) -> None:
    response = client.get("/openapi.json")
    response.raise_for_status()
    spec = response.json()
    assert "/health" in spec["paths"]
    assert "/health/ready" in spec["paths"]
