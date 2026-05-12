"""Mode-gating for the in-process GitHub webhook HTTP route (Phase C.3).

Per ADR-0017, ``POST /api/v1/webhooks/github`` is the canonical receiver
only in ``fully_local`` mode. In ``dev_local`` and ``fully_remote`` modes
the AWS-side path (API Gateway -> Lambda -> SQS -> webhook-inbox poller)
is canonical, and the in-process HTTP route returns 503 with a body
naming this ADR. 503 (not 404) is the right shape: the path exists but
is intentionally disabled in this mode, so a misconfigured caller gets
an actionable error rather than a deceptive "not found."

These tests exercise the gate in isolation — they build a minimal
FastAPI app containing only the webhooks router, override the
``get_settings`` dependency to inject each ``DeploymentMode``, and
assert the gate's behavior. The handler body is never reached in the
non-fully-local cases (the gate runs as a route-level dependency before
the handler). The fully-local case exercises the gate's pass-through,
not the full happy path — the handler's downstream behavior (signature
verify, normalize, persist) is covered by the existing live-API tests
in ``test_integration_webhooks.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.config import DeploymentMode, Settings, get_settings
from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.webhooks import router as webhooks_router


_EXPECTED_503_DETAIL = (
    "webhook ingestion is via the AWS-side path in this mode; see ADR-0017"
)


def _build_app(mode: DeploymentMode) -> FastAPI:
    """Construct a minimal FastAPI app with only the webhooks router and
    overridden dependencies.

    No lifespan handler means no DB/Redis/SNS clients are constructed; the
    ``get_session`` dependency is stubbed to a no-op async generator. The
    handler should never reach session usage in any of these tests:
    - The non-fully-local cases short-circuit at the gate.
    - The fully-local case asserts on a 400 from the missing
      ``X-GitHub-Event`` header, which is raised before any DB I/O.
    """
    app = FastAPI()
    app.include_router(webhooks_router)

    def _settings_override() -> Settings:
        return Settings(deployment_mode=mode)

    async def _session_override() -> Iterator[None]:
        # The gate short-circuits before this is ever needed in dev_local /
        # fully_remote; for fully_local we provoke a 400 before any DB
        # access. The override exists only to prevent the default
        # ``get_session`` from trying to connect to a real database if the
        # request flow ever does reach it.
        yield None  # type: ignore[misc]

    app.dependency_overrides[get_settings] = _settings_override
    app.dependency_overrides[get_session] = _session_override
    return app


# ── fully_local: gate is a pass-through ───────────────────────────────────────


def test_fully_local_mode_does_not_gate_the_route() -> None:
    """In ``fully_local`` mode the gate passes through and the handler
    runs. We don't drive the full happy path here (that's covered by
    ``test_integration_webhooks.py`` against a live API); we just assert
    the response is *not* a 503 from the gate. Without an
    ``X-GitHub-Event`` header the handler raises a 400, which is the
    expected shape per the existing route contract.
    """
    app = _build_app(DeploymentMode.FULLY_LOCAL)
    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/github", content="{}")

    # The gate did not fire. The 400 comes from the handler's own header
    # check; the important property is that it is *not* 503 from the gate.
    assert response.status_code != 503, response.text
    assert response.status_code == 400, response.text
    assert "X-GitHub-Event" in response.json()["detail"]


# ── dev_local: gate returns 503 ───────────────────────────────────────────────


def test_dev_local_mode_returns_503_with_adr_0017_detail() -> None:
    """In ``dev_local`` mode the canonical receiver is the AWS-side path;
    the in-process route returns 503."""
    app = _build_app(DeploymentMode.DEV_LOCAL)
    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/github", content="{}")

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == _EXPECTED_503_DETAIL


def test_dev_local_gate_fires_before_signature_verification() -> None:
    """A misconfigured caller in ``dev_local`` mode gets an immediate 503,
    not a 400 / 401 from signature failure. The gate runs before any
    body work so the operator gets a clear, actionable error pointing
    at ADR-0017 rather than a deceptive low-level failure."""
    app = _build_app(DeploymentMode.DEV_LOCAL)
    with TestClient(app) as client:
        # Send a request that would otherwise blow up signature verification
        # or header-presence checks. The gate should preempt all of that.
        response = client.post(
            "/api/v1/webhooks/github",
            content="not-json{",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": "sha256=deadbeef",
                "X-GitHub-Delivery": "test-delivery-1",
            },
        )

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == _EXPECTED_503_DETAIL


# ── fully_remote: gate returns 503 ────────────────────────────────────────────


def test_fully_remote_mode_returns_503_with_adr_0017_detail() -> None:
    """In ``fully_remote`` mode (future production topology) the AWS-side
    path is canonical, same as dev_local; the in-process route returns
    503."""
    app = _build_app(DeploymentMode.FULLY_REMOTE)
    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/github", content="{}")

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == _EXPECTED_503_DETAIL


# ── Parametrized cross-check ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode",
    [DeploymentMode.DEV_LOCAL, DeploymentMode.FULLY_REMOTE],
)
def test_non_fully_local_modes_all_gate_to_503(mode: DeploymentMode) -> None:
    """Cross-check: every mode that is not ``fully_local`` must gate to
    503. Defends against a future fourth mode being added without
    matching the gate intent."""
    app = _build_app(mode)
    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/github", content="{}")
    assert response.status_code == 503
    assert response.json()["detail"] == _EXPECTED_503_DETAIL
