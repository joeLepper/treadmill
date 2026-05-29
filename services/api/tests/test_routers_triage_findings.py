"""Unit tests for ``POST /api/v1/triage/findings`` — ADR-0061 Step 7.

Exercises the route handler directly with a stub async session. The
``TriageStore.insert_finding`` seam is patched out so the SQL round-trip
is replaced by a recording mock. Mirrors ``test_routers_triage_labels.py``
in style — hermetic, fast, pins the HTTP contract.

Coverage:

  * POST happy path — single TriageFinding accepted, returns 201 +
    finding_ids array with the inserted UUID.
  * POST batch happy path — array of N findings accepted, all
    inserted in input order.
  * POST invalid finding (bad category enum) — 422 from Pydantic
    before any DB write.
  * POST suppression_signal/dispatch_action mismatch — 422 from the
    schema's model_validator.
  * POST UUID collision on finding_id — 409 from the IntegrityError
    handler; transaction rolled back.
  * POST empty body — 422 from the min_length=1 Pydantic constraint.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.triage import findings as findings_mod


# ── Stub session ──────────────────────────────────────────────────────────────


class _StubSession:
    """In-memory stand-in.

    The findings router invokes ``session.commit()`` (once per request)
    and ``session.rollback()`` (only on IntegrityError). ``execute`` is
    a defensive no-op — the store seam is patched so no SQL flows here.
    """

    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.flush = AsyncMock()

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "test stub: TriageStore was meant to be patched out — session.execute "
            "should not be called from the findings router under unit tests"
        )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _finding_payload(
    *,
    finding_id: str | None = None,
    category: str = "layout_overflow",
    dispatch_action: str = "research_only",
    suppression_signal: str | None = None,
    dispatched_plan_id: str | None = None,
) -> dict[str, Any]:
    """Minimal valid TriageFinding payload — every required field
    populated, ready to round-trip through the schema."""
    fid = finding_id or str(uuid.uuid4())
    return {
        "finding_id": fid,
        "run_id": str(uuid.uuid4()),
        "prompt_version": "v1.1.0",
        "model": "claude-sonnet-4-6",
        "mode": "on_demand",
        "on_demand_request": "manual triage of dashboard",
        "target_url": "http://localhost:5174/",
        "viewport_w": 1440,
        "viewport_h": 900,
        "git_sha": "abc1234",
        "api_git_sha": None,
        "screenshot_uri": f"s3://corpus/triage/runs/test/{fid}/screen.png",
        "viewport_png_uri": None,
        "dom_snapshot_uri": None,
        "console_log_uri": f"s3://corpus/triage/runs/test/{fid}/console.log",
        "network_log_uri": f"s3://corpus/triage/runs/test/{fid}/network.log",
        "evidence_summary": {
            "console_errors": 0, "http_4xx": 0, "http_5xx": 0, "requestfailed": 0,
        },
        "category": category,
        "severity": "medium",
        "confidence": "high",
        "observation": "Escalation strip dominates the page.",
        "evidence_pointer": "screen.png:y=80-900",
        "proposed_resolution": (
            "Cap escalation strip at max-height: 240px with overflow-y: auto "
            "per DESIGN.md scroll primitive."
        ),
        "dispatch_action": dispatch_action,
        "dispatch_reason": "Severity medium, confidence high.",
        "suppression_signal": suppression_signal,
        "parent_finding_id": None,
        "dispatched_plan_id": dispatched_plan_id,
    }


def _build_app(
    session: _StubSession,
    *,
    insert_returns: list[uuid.UUID] | None = None,
    insert_raises: Exception | None = None,
) -> FastAPI:
    """Build a FastAPI app with the findings router mounted + the
    TriageStore.insert_finding seam patched."""
    app = FastAPI()
    app.include_router(findings_mod.router, prefix="/api/v1/triage")

    async def _override_get_session() -> Any:
        yield session

    app.dependency_overrides[get_session] = _override_get_session

    # Patch the store's insert_finding directly.
    insert_mock = AsyncMock()
    if insert_raises is not None:
        insert_mock.side_effect = insert_raises
    elif insert_returns is not None:
        insert_mock.side_effect = insert_returns
    else:
        insert_mock.side_effect = lambda *_a, **_k: uuid.uuid4()

    # Patch class method so any TriageStore() instance uses it.
    findings_mod.TriageStore.insert_finding = insert_mock  # type: ignore[method-assign]
    app.state.insert_mock = insert_mock
    return app


# ── Tests ────────────────────────────────────────────────────────────────────


def test_create_findings_single_happy_path() -> None:
    """A POST with one valid finding lands a 201 + the finding_id."""
    session = _StubSession()
    fid = uuid.uuid4()
    app = _build_app(session, insert_returns=[fid])

    payload = _finding_payload(finding_id=str(fid))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/triage/findings", json={"findings": [payload]},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["finding_ids"] == [str(fid)]
    assert session.commit.await_count == 1


def test_create_findings_batch_inserts_in_order() -> None:
    """A POST with N findings inserts each one in the input order."""
    session = _StubSession()
    fids = [uuid.uuid4() for _ in range(3)]
    app = _build_app(session, insert_returns=list(fids))

    payloads = [_finding_payload(finding_id=str(f)) for f in fids]
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/triage/findings", json={"findings": payloads},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["count"] == 3
    assert body["finding_ids"] == [str(f) for f in fids]
    assert app.state.insert_mock.await_count == 3
    assert session.commit.await_count == 1


def test_create_findings_rejects_unknown_category() -> None:
    """A category outside the closed enum is rejected at the Pydantic
    layer before any DB write."""
    session = _StubSession()
    app = _build_app(session)

    payload = _finding_payload(category="not_a_real_category")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/triage/findings", json={"findings": [payload]},
        )

    assert response.status_code == 422
    assert app.state.insert_mock.await_count == 0
    assert session.commit.await_count == 0


def test_create_findings_enforces_suppression_signal_validator() -> None:
    """suppression_signal must be null unless dispatch_action == 'suppressed'
    — the schema's model_validator catches the mismatch before insert."""
    session = _StubSession()
    app = _build_app(session)

    # dispatch_action='dispatched' + a non-null suppression_signal is invalid.
    payload = _finding_payload(
        dispatch_action="dispatched",
        suppression_signal="design_intent",
        dispatched_plan_id=str(uuid.uuid4()),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/triage/findings", json={"findings": [payload]},
        )

    assert response.status_code == 422
    assert app.state.insert_mock.await_count == 0


def test_create_findings_409_on_uuid_collision() -> None:
    """An IntegrityError (e.g. PK collision on finding_id) rolls the
    whole batch back and surfaces as 409."""
    session = _StubSession()
    app = _build_app(
        session,
        insert_raises=IntegrityError("INSERT", {}, Exception("duplicate key")),
    )

    payload = _finding_payload()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/triage/findings", json={"findings": [payload]},
        )

    assert response.status_code == 409, response.text
    assert "duplicate key" in response.text.lower() or "failed" in response.text.lower()
    assert session.rollback.await_count == 1
    assert session.commit.await_count == 0


def test_create_findings_rejects_empty_array() -> None:
    """min_length=1 on the request model means a no-finding POST fails
    at validation."""
    session = _StubSession()
    app = _build_app(session)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/triage/findings", json={"findings": []},
        )

    assert response.status_code == 422
    assert app.state.insert_mock.await_count == 0
