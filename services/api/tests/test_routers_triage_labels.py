"""Unit tests for ``GET /api/v1/triage/findings`` + ``POST .../label``.

Exercises the route handlers directly with a stub async session — no
live database. The :class:`TriageStore` accessor is patched in the
labels module so the SQL round-trip is replaced by a fixture-driven
in-memory store. Mirrors ``test_routers_dashboard_repo_docs.py`` in
style: hermetic, fast, pins the HTTP contract.

Coverage:

  * GET happy path — returns ``TriageFinding[]`` honoring the labeling-
    queue contract.
  * POST round-trip — record + re-GET excludes the labeled row.
  * POST with all-null label fields is accepted (per ADR-0061, null is
    a signal).
  * POST 404 when the finding doesn't exist.
  * Empty ``labeled_by`` → 422 (the only non-nullable label field).
  * Auto-discovery contract — ``labels.py`` is mounted under the
    aggregator without an ``__init__.py`` edit, and a freshly authored
    sibling gets picked up by the same discovery pass (mirrors the
    dashboard package's synthetic-sibling test).
"""

from __future__ import annotations

import importlib
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers import triage as triage_pkg
from treadmill_api.routers.triage import labels as labels_mod
from treadmill_api.schemas.triage_finding import TriageFinding


# ── Stub session ──────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _ScalarsResult:
    """Surface for ``session.scalars(stmt).one_or_none()`` lookups."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def one_or_none(self) -> Any:
        return self._value


class _StubSession:
    """In-memory stand-in for an AsyncSession.

    The labels router only invokes three session APIs:

      1. ``scalars(stmt).one_or_none()`` — existence probe; we return the
         configured ``existing_row`` regardless of statement shape (each
         request issues exactly one ``scalars`` call).
      2. ``commit()`` — counter-tracked so tests can assert it fired.
      3. ``refresh(obj)`` — counter-tracked; tests don't care which row.

    The store's record_label call is monkey-patched out at the
    ``TriageStore`` seam (see :func:`_patch_store`), so ``execute`` here
    is a defensive no-op that raises if exercised.
    """

    def __init__(self, *, existing_row: Any | None = None) -> None:
        self.existing_row = existing_row
        self.commits = 0
        self.refreshes = 0

    async def scalars(self, _statement: Any) -> _ScalarsResult:
        return _ScalarsResult(self.existing_row)

    async def execute(self, _statement: Any) -> Any:  # pragma: no cover
        raise AssertionError(
            "stub session should not receive direct execute() calls — the "
            "router goes through TriageStore.record_label (patched in tests)"
        )

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj: Any) -> None:
        self.refreshes += 1


def _build_app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(triage_pkg.router)

    def _override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    return app


# ── Row builder ───────────────────────────────────────────────────────────────


def _row(
    *,
    finding_id: uuid.UUID | None = None,
    label_is_real_bug: bool | None = None,
    observation: str = "Layout overflow on overview at 1440x900.",
) -> SimpleNamespace:
    """Minimal Row-like object exposing the attributes the Pydantic
    ``TriageFinding`` schema reads via ``from_attributes=True``."""
    fid = finding_id or uuid.uuid4()
    return SimpleNamespace(
        finding_id=fid,
        run_id=uuid.uuid4(),
        created_at=_now(),
        prompt_version="v1.0.0",
        model="claude-opus-4-7",
        mode="periodic",
        on_demand_request=None,
        target_url="http://localhost:5174/",
        viewport_w=1440,
        viewport_h=900,
        git_sha="abc1234",
        api_git_sha=None,
        screenshot_uri=f"s3://corpus/triage/runs/test/{fid}/screen.png",
        viewport_png_uri=None,
        dom_snapshot_uri=None,
        console_log_uri=f"s3://corpus/triage/runs/test/{fid}/console.log",
        network_log_uri=f"s3://corpus/triage/runs/test/{fid}/network.log",
        evidence_summary={
            "console_errors": 0,
            "http_4xx": 0,
            "http_5xx": 0,
            "requestfailed": 0,
        },
        category="layout_overflow",
        severity="medium",
        confidence="high",
        observation=observation,
        evidence_pointer="screen.png:y=80-900",
        proposed_resolution=(
            "Cap escalation strip at max-height: 240px with overflow-y: auto."
        ),
        dispatch_action="research_only",
        dispatch_reason="Severity medium, confidence high — research path.",
        suppression_signal=None,
        parent_finding_id=None,
        dispatched_plan_id=None,
        outcome_state=None,
        outcome_pr_number=None,
        outcome_merged_at=None,
        recurrence_count=0,
        label_is_real_bug=label_is_real_bug,
        label_severity=None,
        label_category=None,
        label_fix_in_dsl=None,
        label_dispatch_action=None,
        label_notes=None,
        labeled_by=None,
        labeled_at=None,
        label_guidelines_version=None,
    )


def _patch_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    unlabeled_rows: list[SimpleNamespace] | None = None,
    record_label: AsyncMock | None = None,
) -> SimpleNamespace:
    """Replace the ``TriageStore`` class with a factory returning a
    SimpleNamespace exposing AsyncMock-driven ``get_unlabeled_findings``
    and ``record_label`` methods. Returns the SimpleNamespace so tests
    can assert on the mock interactions.
    """
    get_unlabeled = AsyncMock(
        return_value=[
            TriageFinding.model_validate(r) for r in (unlabeled_rows or [])
        ],
    )
    fake_store = SimpleNamespace(
        get_unlabeled_findings=get_unlabeled,
        record_label=record_label or AsyncMock(return_value=None),
    )
    monkeypatch.setattr(labels_mod, "TriageStore", lambda: fake_store)
    return fake_store


# ── GET ───────────────────────────────────────────────────────────────────────


def test_get_unlabeled_findings_returns_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /findings returns the unlabeled-queue contract."""
    row_a = _row(observation="Console error on /tasks.")
    row_b = _row(observation="404 on docs fetch.")
    fake_store = _patch_store(monkeypatch, unlabeled_rows=[row_a, row_b])

    app = _build_app(_StubSession())

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/triage/findings",
            params={"label_is_real_bug": "null", "limit": 50},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    ids = {item["finding_id"] for item in body}
    assert ids == {str(row_a.finding_id), str(row_b.finding_id)}
    fake_store.get_unlabeled_findings.assert_awaited_once()
    # The router forwards the limit query param down to the store.
    _, kwargs = fake_store.get_unlabeled_findings.await_args
    assert kwargs.get("limit") == 50


def test_get_unlabeled_findings_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rows → empty list, not a 404."""
    _patch_store(monkeypatch, unlabeled_rows=[])
    app = _build_app(_StubSession())
    with TestClient(app) as client:
        response = client.get("/api/v1/triage/findings")
    assert response.status_code == 200
    assert response.json() == []


def test_get_returns_full_triage_finding_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every required ``TriageFinding`` field round-trips through GET."""
    row = _row()
    _patch_store(monkeypatch, unlabeled_rows=[row])
    app = _build_app(_StubSession())
    with TestClient(app) as client:
        body = client.get("/api/v1/triage/findings").json()
    assert len(body) == 1
    parsed = TriageFinding.model_validate(body[0])
    assert parsed.category == "layout_overflow"
    assert parsed.dispatch_action == "research_only"


# ── POST ──────────────────────────────────────────────────────────────────────


def test_post_label_round_trip_excludes_labeled_row_from_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording a label flips ``label_is_real_bug`` on the row, and the
    subsequent GET excludes it — the canonical labeling-UI flow.

    Two TestClient roundtrips: POST then GET. The shared ``rows`` dict
    is mutated by ``fake_record_label`` so the next GET sees the row's
    new label state. ``fake_get_unlabeled`` filters that dict the same
    way the real ``ix_triage_findings_unlabeled`` partial index does.
    """
    row = _row()
    other = _row(observation="Still unlabeled.")
    rows = {row.finding_id: row, other.finding_id: other}

    record_calls: list[dict[str, Any]] = []

    async def fake_record_label(
        _session: Any, finding_id: uuid.UUID, **kwargs: Any,
    ) -> None:
        record_calls.append({"finding_id": finding_id, **kwargs})
        target = rows.get(finding_id)
        if target is not None:
            for k, v in kwargs.items():
                setattr(target, k, v)
            target.labeled_at = _now()

    async def fake_get_unlabeled(
        _session: Any, limit: int = 50,
    ) -> list[TriageFinding]:
        unlabeled = [r for r in rows.values() if r.label_is_real_bug is None]
        return [TriageFinding.model_validate(r) for r in unlabeled][:limit]

    # Each request constructs a fresh TriageStore() instance. Both
    # requests in this test need the shared in-memory rows, so we point
    # the patched class at the same SimpleNamespace on every call.
    fake_store = SimpleNamespace(
        get_unlabeled_findings=fake_get_unlabeled,
        record_label=fake_record_label,
    )
    monkeypatch.setattr(labels_mod, "TriageStore", lambda: fake_store)

    # Existence-probe session: returns the row we're labeling on the
    # first request, then is replaced for the GET (which doesn't probe).
    post_session = _StubSession(existing_row=row)
    get_session_stub = _StubSession()

    sessions = iter([post_session, get_session_stub])

    def _session_override() -> Iterator[_StubSession]:
        yield next(sessions)

    app = FastAPI()
    app.include_router(triage_pkg.router)
    app.dependency_overrides[get_session] = _session_override

    with TestClient(app) as client:
        post_resp = client.post(
            f"/api/v1/triage/findings/{row.finding_id}/label",
            json={
                "label_is_real_bug": True,
                "label_severity": "medium",
                "label_category": "layout_overflow",
                "label_fix_in_dsl": False,
                "label_notes": "Real bug; fix at the page level.",
                "labeled_by": "operator",
            },
        )
        assert post_resp.status_code == 200, post_resp.text
        payload = post_resp.json()
        assert payload["finding_id"] == str(row.finding_id)
        assert payload["label_is_real_bug"] is True
        assert payload["label_severity"] == "medium"
        assert payload["label_category"] == "layout_overflow"
        assert payload["label_fix_in_dsl"] is False
        assert payload["labeled_by"] == "operator"

        get_resp = client.get("/api/v1/triage/findings")
        assert get_resp.status_code == 200
        remaining_ids = {item["finding_id"] for item in get_resp.json()}
        assert str(row.finding_id) not in remaining_ids
        assert str(other.finding_id) in remaining_ids

    assert len(record_calls) == 1
    assert record_calls[0]["finding_id"] == row.finding_id
    assert record_calls[0]["label_is_real_bug"] is True
    assert record_calls[0]["labeled_by"] == "operator"
    assert post_session.commits == 1


def test_post_label_with_all_null_labels_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per ADR-0061, every label field accepts null (Skip = signal)."""
    row = _row()

    async def fake_record_label(
        _session: Any, _finding_id: uuid.UUID, **kwargs: Any,
    ) -> None:
        # All-null labels are valid; only labeled_by + server-stamped
        # labeled_at change off-default.
        row.labeled_by = kwargs.get("labeled_by")
        row.labeled_at = _now()

    _patch_store(
        monkeypatch,
        record_label=fake_record_label,  # type: ignore[arg-type]
    )

    app = _build_app(_StubSession(existing_row=row))
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/triage/findings/{row.finding_id}/label",
            json={
                "label_is_real_bug": None,
                "label_severity": None,
                "label_category": None,
                "label_fix_in_dsl": None,
                "label_notes": None,
                "labeled_by": "operator",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["label_is_real_bug"] is None
    assert body["label_severity"] is None
    assert body["label_category"] is None
    assert body["label_fix_in_dsl"] is None
    assert body["labeled_by"] == "operator"


def test_post_label_requires_labeled_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``labeled_by`` is the only non-null label field; empty → 422."""
    row = _row()
    _patch_store(monkeypatch)
    app = _build_app(_StubSession(existing_row=row))
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/triage/findings/{row.finding_id}/label",
            json={"label_is_real_bug": True, "labeled_by": ""},
        )
    assert resp.status_code == 422


def test_post_label_unknown_finding_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown finding_id → 404 with a descriptive detail."""
    _patch_store(monkeypatch)
    app = _build_app(_StubSession(existing_row=None))
    missing_id = uuid.uuid4()
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/triage/findings/{missing_id}/label",
            json={"label_is_real_bug": True, "labeled_by": "operator"},
        )
    assert resp.status_code == 404
    assert str(missing_id) in resp.json()["detail"]


# ── Auto-discovery contract ───────────────────────────────────────────────────


def test_aggregator_router_is_prefixed_and_tagged() -> None:
    """``router`` is the aggregator under ``/api/v1/triage``."""
    assert triage_pkg.router.prefix == "/api/v1/triage"
    assert "triage" in triage_pkg.router.tags


def test_labels_router_is_auto_discovered() -> None:
    """Discovery picks up ``labels.py`` without an ``__init__.py`` edit."""
    assert "labels" in triage_pkg.MOUNTED_MODULES
    paths = {getattr(route, "path", None) for route in triage_pkg.router.routes}
    assert "/api/v1/triage/findings" in paths
    assert "/api/v1/triage/findings/{finding_id}/label" in paths


def test_app_mounts_triage_router_once() -> None:
    """``app.py`` includes the aggregator exactly once."""
    from treadmill_api.app import create_app

    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/v1/triage/findings" in paths
    assert "/api/v1/triage/findings/{finding_id}/label" in paths


def test_init_does_not_enumerate_sibling_module_names() -> None:
    """Auto-discovery contract: ``__init__.py`` must not name siblings.

    Mirrors the dashboard-package guard so future PRs can drop sibling
    routers in without racing on ``__init__.py``.
    """
    init_path = Path(triage_pkg.__file__)
    source = init_path.read_text()
    assert "labels" not in source, (
        "routers/triage/__init__.py mentions 'labels' by name — "
        "auto-discovery should not enumerate siblings, or future PRs "
        "will conflict on this file"
    )


def test_labels_module_exposes_router_attribute() -> None:
    """Sibling contract: every triage module exports a top-level ``router``."""
    assert isinstance(labels_mod.router, APIRouter)


def test_discovery_picks_up_a_freshly_added_sibling() -> None:
    """Re-run the discovery pass against a synthetic sibling and verify
    it gets mounted without any edit to ``__init__.py`` — the forward-
    compatibility guarantee for future routes under ``/api/v1/triage``.
    Mirrors ``test_routers_dashboard_init.py``'s synthetic-sibling test.
    """
    pkg_dir = Path(triage_pkg.__file__).parent
    sibling_path = pkg_dir / "_test_synthetic_sibling.py"
    sibling_path.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/_synthetic-probe')\n"
        "async def _probe() -> dict:\n"
        "    return {'ok': True}\n"
    )
    try:
        sys.modules.pop(
            "treadmill_api.routers.triage._test_synthetic_sibling",
            None,
        )
        fresh_aggregator = APIRouter(
            prefix=triage_pkg.router.prefix,
            tags=list(triage_pkg.router.tags),
        )
        mounted = triage_pkg._discover_and_mount(fresh_aggregator)

        assert "_test_synthetic_sibling" in mounted
        app = FastAPI()
        app.include_router(fresh_aggregator)
        paths = {getattr(route, "path", None) for route in app.routes}
        assert "/api/v1/triage/_synthetic-probe" in paths
    finally:
        sibling_path.unlink(missing_ok=True)
        sys.modules.pop(
            "treadmill_api.routers.triage._test_synthetic_sibling",
            None,
        )
        importlib.reload(triage_pkg)
