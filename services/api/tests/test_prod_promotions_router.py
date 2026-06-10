"""Unit tests for the prod-promotion gate router (ADR-0088).

The stub session implements compare-and-swap semantics faithfully
(status + expiry checked against the in-memory row before "updating")
so these tests exercise the router's real guard handling — per the
equivalence-harness rule, the stub must never mock away the asserted
property. The exact SQL guard is additionally pinned against live
Postgres in ``test_integration_prod_promotions.py``.

Coverage:
* operator-key gate: 503 unconfigured (fails closed), 403 wrong/missing,
  success with the right key
* propose → 201, status=proposed, ``prod_promotion.proposed`` emitted
* approve happy path; idempotent re-approve; 409 on rejected
* expired proposal not approvable (CAS guard) + lazy-expiry flip on GET
* reject requires reason
* transition state machine: started requires approved; succeeded
  requires started; unknown action 422; failed requires reason
* every new payload class round-trips through the real registry
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.config import get_settings
from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import get_dispatcher
from treadmill_api.events.registry import encode_payload, parse_payload
from treadmill_api.routers.prod_promotions import router


def _bundle(expires_in_hours: float = 48.0) -> dict[str, Any]:
    return {
        "repo": "acme/widget",
        "env_from": "staging",
        "env_to": "prod",
        "digests": [{"service": "api", "digest": "sha256:" + "a" * 64}],
        "staging_evidence": {
            "deploy_event_id": str(uuid.uuid4()),
            "smoke_event_id": str(uuid.uuid4()),
            "sha": "b" * 40,
            "smoke_passed_at": datetime.now(timezone.utc).isoformat(),
        },
        "diff_summary": ["#101", "#102"],
        "diff_anchor": "genesis:" + "b" * 40,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
        ).isoformat(),
        "proposed_by": "coordinator-acme-widget",
    }


class _Row:
    def __init__(self, bundle: dict[str, Any], expires_at: datetime) -> None:
        self.proposal_id = uuid.uuid4()
        self.repo = bundle["repo"]
        self.status = "proposed"
        self.bundle = bundle
        self.expires_at = expires_at
        self.decided_by: str | None = None
        self.decided_at: datetime | None = None
        self.decision_note: str | None = None
        self.created_at = datetime.now(timezone.utc)


class _Result:
    def __init__(self, hit: bool) -> None:
        self._hit = hit

    def first(self) -> Any:
        return object() if self._hit else None

    def scalars(self) -> Any:
        return self

    def all(self) -> list[Any]:
        return []


class _StubSession:
    """In-memory session with FAITHFUL CAS semantics.

    ``execute(update(...))`` evaluates the same predicates the real SQL
    guard does — status equality and (when present in the values/where
    shape) expiry — against the stored row, then applies the values only
    on a hit. The asserted property (single-use, expiry) is therefore
    real logic, not a mock's return value.
    """

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _Row] = {}
        self.added: list[Any] = []
        self.commits = 0

    def seed(self, bundle: dict[str, Any], *, expires_at: datetime) -> _Row:
        row = _Row(bundle, expires_at)
        self.rows[row.proposal_id] = row
        return row

    async def get(self, model: Any, pk: uuid.UUID) -> Any:
        return self.rows.get(pk)

    def add(self, obj: Any) -> None:
        # propose path: assign server defaults the DB would
        obj.proposal_id = uuid.uuid4()
        obj.status = "proposed"
        obj.decided_by = None
        obj.decided_at = None
        obj.decision_note = None
        obj.created_at = datetime.now(timezone.utc)
        self.added.append(obj)
        self.rows[obj.proposal_id] = obj

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        pass

    async def execute(self, stmt: Any) -> _Result:
        # Only UPDATE ... WHERE reaches here (list endpoint returns no rows).
        from sqlalchemy.dialects import postgresql

        compiled = str(
            stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": False},
            )
        )
        params = stmt.compile(dialect=postgresql.dialect()).params
        target = None
        for row in self.rows.values():
            if row.proposal_id == params.get("proposal_id_1"):
                target = row
                break
        if target is None:
            return _Result(False)
        expected_status = params.get("status_1")
        if target.status != expected_status:
            return _Result(False)
        if "expires_at" in compiled and target.expires_at <= datetime.now(
            timezone.utc
        ):
            return _Result(False)
        # apply SET values
        for key, value in params.items():
            if key in {"status", "decided_by", "decided_at", "decision_note"}:
                setattr(target, key, value)
        return _Result(True)


class _StubDispatcher:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, str, Any]] = []

    async def persist_and_publish(
        self, session: Any, *, entity_type: str, action: str, payload: Any, **kw: Any
    ) -> Any:
        # Round-trip through the REAL registry so payload-shape drift fails
        # here, not in production (encode validates via the payload class).
        encode_payload(payload)
        self.emitted.append((entity_type, action, payload))
        return object()


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TREADMILL_OPERATOR_KEY", "test-operator-key")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)
    session = _StubSession()
    dispatcher = _StubDispatcher()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    client = TestClient(app)
    yield client, session, dispatcher
    get_settings.cache_clear()


KEY = {"X-Operator-Key": "test-operator-key"}
DECIDE = {"decided_by": "joe"}


class TestOperatorKeyGate:
    def test_unconfigured_gate_fails_closed(self, monkeypatch):
        monkeypatch.delenv("TREADMILL_OPERATOR_KEY", raising=False)
        get_settings.cache_clear()
        app = FastAPI()
        app.include_router(router)
        session = _StubSession()
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_dispatcher] = lambda: _StubDispatcher()
        client = TestClient(app)
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/approve",
            json=DECIDE,
            headers={"X-Operator-Key": "anything"},
        )
        assert resp.status_code == 503
        get_settings.cache_clear()

    def test_wrong_key_403(self, harness):
        client, session, _ = harness
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/approve",
            json=DECIDE,
            headers={"X-Operator-Key": "wrong"},
        )
        assert resp.status_code == 403

    def test_missing_key_403(self, harness):
        client, session, _ = harness
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/approve", json=DECIDE
        )
        assert resp.status_code == 403


class TestProposeAndApprove:
    def test_propose_creates_and_emits(self, harness):
        client, session, dispatcher = harness
        resp = client.post("/api/v1/prod_promotions", json=_bundle())
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "proposed"
        assert dispatcher.emitted[0][:2] == ("prod_promotion", "proposed")

    def test_approve_happy_path(self, harness):
        client, session, dispatcher = harness
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/approve",
            json=DECIDE,
            headers=KEY,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        assert ("prod_promotion", "approved") in [e[:2] for e in dispatcher.emitted]

    def test_reapprove_is_idempotent(self, harness):
        client, session, dispatcher = harness
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        first = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/approve",
            json=DECIDE,
            headers=KEY,
        )
        assert first.status_code == 200
        emitted_before = len(dispatcher.emitted)
        second = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/approve",
            json=DECIDE,
            headers=KEY,
        )
        assert second.status_code == 200
        assert second.json()["status"] == "approved"
        # No second approved event — idempotent return, not a transition.
        assert len(dispatcher.emitted) == emitted_before

    def test_expired_not_approvable(self, harness):
        client, session, _ = harness
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/approve",
            json=DECIDE,
            headers=KEY,
        )
        assert resp.status_code == 409
        assert "expired" in resp.json()["detail"]

    def test_lazy_expiry_on_get(self, harness):
        client, session, dispatcher = harness
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        resp = client.get(f"/api/v1/prod_promotions/{row.proposal_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "expired"
        assert ("prod_promotion", "expired") in [e[:2] for e in dispatcher.emitted]

    def test_reject_requires_reason(self, harness):
        client, session, _ = harness
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/reject",
            json=DECIDE,
            headers=KEY,
        )
        assert resp.status_code == 422

    def test_approve_after_reject_409(self, harness):
        client, session, _ = harness
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        rej = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/reject",
            json={"decided_by": "joe", "reason": "stale evidence"},
            headers=KEY,
        )
        assert rej.status_code == 200
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/approve",
            json=DECIDE,
            headers=KEY,
        )
        assert resp.status_code == 409


class TestTransitions:
    def _approved_row(self, session):
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        row.status = "approved"
        return row

    def test_started_requires_approved(self, harness):
        client, session, _ = harness
        row = session.seed(
            _bundle(), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/transition",
            json={"action": "started"},
        )
        assert resp.status_code == 409

    def test_full_lifecycle(self, harness):
        client, session, dispatcher = harness
        row = self._approved_row(session)
        start = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/transition",
            json={"action": "started", "workflow_run_id": "123"},
        )
        assert start.status_code == 200
        done = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/transition",
            json={
                "action": "succeeded",
                "sha": "b" * 40,
                "digests": [{"service": "api", "digest": "sha256:" + "a" * 64}],
            },
        )
        assert done.status_code == 200
        assert done.json()["status"] == "succeeded"
        actions = [e[1] for e in dispatcher.emitted]
        assert actions == ["started", "succeeded"]

    def test_succeeded_requires_started(self, harness):
        client, session, _ = harness
        row = self._approved_row(session)
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/transition",
            json={
                "action": "succeeded",
                "sha": "b" * 40,
                "digests": [],
            },
        )
        assert resp.status_code == 409

    def test_failed_requires_reason(self, harness):
        client, session, _ = harness
        row = self._approved_row(session)
        row.status = "started"
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/transition",
            json={"action": "failed"},
        )
        assert resp.status_code == 422

    def test_unknown_action_422(self, harness):
        client, session, _ = harness
        row = self._approved_row(session)
        resp = client.post(
            f"/api/v1/prod_promotions/{row.proposal_id}/transition",
            json={"action": "redeploy"},
        )
        assert resp.status_code == 422


class TestPayloadRegistry:
    """Every new payload class round-trips through the real registry —
    the consumer-contract test for the vocabulary."""

    @pytest.mark.parametrize(
        ("entity_type", "action", "payload"),
        [
            (
                "deploy",
                "succeeded",
                {
                    "repo": "acme/widget",
                    "env": "staging",
                    "sha": "b" * 40,
                    "digests": [{"service": "api", "digest": "sha256:" + "a" * 64}],
                },
            ),
            (
                "deploy",
                "failed",
                {"repo": "acme/widget", "env": "staging", "sha": "b" * 40, "reason": "x"},
            ),
            (
                "staging_smoke",
                "passed",
                {"repo": "acme/widget", "sha": "b" * 40, "run_url": None},
            ),
            (
                "staging_smoke",
                "failed",
                {"repo": "acme/widget", "sha": "b" * 40, "reason": "parity", "run_url": None},
            ),
            (
                "prod_promotion",
                "failed",
                {"proposal_id": str(uuid.uuid4()), "repo": "acme/widget", "reason": "digest_mismatch"},
            ),
        ],
    )
    def test_round_trip(self, entity_type, action, payload):
        decoded = parse_payload(entity_type, action, payload)
        encoded = encode_payload(decoded)
        assert parse_payload(entity_type, action, encoded) == decoded
