"""Unit tests for the architect-gold review-queue router (ADR-0070 substep 3).

Exercises route handlers with a stub async session — no live database. Mirrors
``test_routers_triage_labels.py`` in style: hermetic, fast, pins the HTTP
contract and the auto-discovery wiring.

Coverage:

  * GET /next returns rows in the expected confidence order (low → medium → high).
  * POST /{id}/label round-trips; the row transitions to non-null ``labeled_at``.
  * POST 404 when the row id is absent.
  * POST 409 when the row is already labeled.
  * POST 422 when ``label`` is not in the closed set (Pydantic enforces this).
  * GET /stats reports ``label_accuracy = matched / labeled`` correctly.
  * Auto-discovery contract: ``review_architect_gold`` appears in
    ``review_pkg.MOUNTED_MODULES`` and the review aggregator mounts the
    architect-gold paths.
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

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers import review as review_pkg
from treadmill_api.routers.review import review_architect_gold as ag_mod


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Stub session ──────────────────────────────────────────────────────────────


class _ScalarsResult:
    """Minimal surface for ``session.scalars(stmt).one_or_none()`` and ``.all()``."""

    def __init__(self, rows: list[Any] | Any) -> None:
        self._rows: list[Any] = rows if isinstance(rows, list) else [rows]

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _StubSession:
    """In-memory stand-in for ``AsyncSession``.

    Configured with a list of rows returned for every ``scalars()`` call.
    ``commit()`` and ``refresh()`` are tracked counters; ``refresh`` also
    forwards any pending attribute mutations to simulate the server
    round-trip.
    """

    def __init__(self, *, rows: list[Any] | None = None) -> None:
        self._rows: list[Any] = rows or []
        self.commits = 0
        self.refreshes = 0

    async def scalars(self, _stmt: Any) -> _ScalarsResult:
        return _ScalarsResult(list(self._rows))

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj: Any) -> None:
        self.refreshes += 1


def _build_app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(review_pkg.router)

    def _override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    return app


# ── Row builder ───────────────────────────────────────────────────────────────


def _row(
    *,
    row_id: uuid.UUID | None = None,
    llm_confidence: str = "high",
    label_verdict: str | None = None,
    labeled_at: datetime | None = None,
    llm_label: str = "correct",
    verdict_emitted: str = "accept-as-is",
) -> SimpleNamespace:
    """Minimal Row-like object with all attributes the Pydantic response model reads."""
    rid = row_id or uuid.uuid4()
    return SimpleNamespace(
        id=rid,
        created_at=_now(),
        source_run_id=None,
        source_event_id=None,
        source_task_id=None,
        source_pr_number=None,
        source_url=None,
        decision_id=f"plan/{rid}",
        verdict_emitted=verdict_emitted,
        rationale_excerpt="The change looks reasonable.",
        gate_log_uri=None,
        llm_label=llm_label,
        llm_confidence=llm_confidence,
        llm_rationale="Matches established patterns.",
        llm_prompt_version="v1.0.0",
        llm_model="claude-sonnet-4-6",
        label_verdict=label_verdict,
        label_notes=None,
        label_override_reason=None,
        labeled_by="operator" if label_verdict else None,
        labeled_at=labeled_at or (_now() if label_verdict else None),
        label_guidelines_version=None,
        outcome_state=None,
        outcome_pr_merged_at=None,
    )


# ── GET /next ─────────────────────────────────────────────────────────────────


def test_get_next_returns_rows_in_confidence_order() -> None:
    """Rows seeded in [low, medium, high] confidence order appear in that order.

    The stub returns rows in the order the test seeds them (simulating DB-side
    ORDER BY on the CASE expression). The response must preserve that order so
    operators see the least-confident proposals first.
    """
    low_row = _row(llm_confidence="low")
    med_row = _row(llm_confidence="medium")
    high_row = _row(llm_confidence="high")

    app = _build_app(_StubSession(rows=[low_row, med_row, high_row]))
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/architect-gold/next", params={"limit": 10})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 3
    assert body[0]["id"] == str(low_row.id)
    assert body[1]["id"] == str(med_row.id)
    assert body[2]["id"] == str(high_row.id)
    assert body[0]["llm_confidence"] == "low"
    assert body[1]["llm_confidence"] == "medium"
    assert body[2]["llm_confidence"] == "high"


def test_get_next_empty_queue_returns_empty_list() -> None:
    """No unlabeled rows → empty list, not 404."""
    app = _build_app(_StubSession(rows=[]))
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/architect-gold/next")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_next_default_limit_is_20() -> None:
    """Default limit parameter is 20."""
    rows = [_row() for _ in range(5)]
    app = _build_app(_StubSession(rows=rows))
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/architect-gold/next")
    assert resp.status_code == 200
    assert len(resp.json()) == 5


# ── POST /{row_id}/label ──────────────────────────────────────────────────────


def test_post_label_round_trip() -> None:
    """Recording a label sets label_verdict + labeled_at on the row."""
    row = _row()
    session = _StubSession(rows=[row])
    app = _build_app(session)

    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/architect-gold/{row.id}/label",
            json={
                "label": "correct",
                "notes": "Looks right to me.",
                "labeled_by": "operator",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(row.id)
    assert body["label_verdict"] == "correct"
    assert body["label_notes"] == "Looks right to me."
    assert body["labeled_by"] == "operator"
    assert body["labeled_at"] is not None

    # commit + refresh must have fired
    assert session.commits == 1
    assert session.refreshes == 1


def test_post_label_with_override_reason() -> None:
    """``override_reason`` is accepted as an optional field."""
    row = _row()
    app = _build_app(_StubSession(rows=[row]))
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/architect-gold/{row.id}/label",
            json={
                "label": "too-strict",
                "override_reason": "LLM was wrong here.",
                "labeled_by": "operator",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["label_verdict"] == "too-strict"
    assert body["label_override_reason"] == "LLM was wrong here."


def test_post_label_404_when_row_absent() -> None:
    """Unknown row_id → 404."""
    missing_id = uuid.uuid4()
    app = _build_app(_StubSession(rows=[]))
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/architect-gold/{missing_id}/label",
            json={"label": "correct", "labeled_by": "operator"},
        )
    assert resp.status_code == 404
    assert str(missing_id) in resp.json()["detail"]


def test_post_label_409_when_already_labeled() -> None:
    """Row already carrying a label_verdict → 409."""
    row = _row(label_verdict="correct")
    app = _build_app(_StubSession(rows=[row]))
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/architect-gold/{row.id}/label",
            json={"label": "too-strict", "labeled_by": "operator"},
        )
    assert resp.status_code == 409
    assert str(row.id) in resp.json()["detail"]


def test_post_label_422_when_label_not_in_closed_set() -> None:
    """``label`` outside the four-value closed set → 422 from Pydantic."""
    row = _row()
    app = _build_app(_StubSession(rows=[row]))
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/architect-gold/{row.id}/label",
            json={"label": "invalid-verdict", "labeled_by": "operator"},
        )
    assert resp.status_code == 422


def test_post_label_422_when_labeled_by_empty() -> None:
    """Empty ``labeled_by`` → 422 (min_length=1 guard)."""
    row = _row()
    app = _build_app(_StubSession(rows=[row]))
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/architect-gold/{row.id}/label",
            json={"label": "correct", "labeled_by": ""},
        )
    assert resp.status_code == 422


# ── GET /{row_id} ─────────────────────────────────────────────────────────────


def test_get_row_returns_full_shape() -> None:
    """GET /{row_id} returns all fields."""
    row = _row(label_verdict="correct")
    app = _build_app(_StubSession(rows=[row]))
    with TestClient(app) as client:
        resp = client.get(f"/api/v1/review/architect-gold/{row.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(row.id)
    assert body["verdict_emitted"] == row.verdict_emitted
    assert body["llm_label"] == row.llm_label


def test_get_row_404_when_absent() -> None:
    """GET /{row_id} returns 404 when the row doesn't exist."""
    missing_id = uuid.uuid4()
    app = _build_app(_StubSession(rows=[]))
    with TestClient(app) as client:
        resp = client.get(f"/api/v1/review/architect-gold/{missing_id}")
    assert resp.status_code == 404


# ── GET /stats ────────────────────────────────────────────────────────────────


def test_get_stats_empty_queue() -> None:
    """Empty corpus → total=0, unlabeled=0, labeled_total=0, accuracies=None."""
    app = _build_app(_StubSession(rows=[]))
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/architect-gold/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["unlabeled"] == 0
    assert body["labeled_total"] == 0
    assert body["label_accuracy"] is None
    assert body["accuracy_last_100"] is None


def test_get_stats_label_accuracy_correct_fraction() -> None:
    """3 matched + 2 mismatched labels → label_accuracy = 0.6."""
    matched_rows = [
        _row(llm_label="correct", label_verdict="correct") for _ in range(3)
    ]
    mismatched_rows = [
        _row(llm_label="correct", label_verdict="too-strict") for _ in range(2)
    ]
    all_rows = matched_rows + mismatched_rows
    app = _build_app(_StubSession(rows=all_rows))
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/architect-gold/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["labeled_total"] == 5
    assert body["unlabeled"] == 0
    assert abs(body["label_accuracy"] - 0.6) < 1e-9
    assert abs(body["accuracy_last_100"] - 0.6) < 1e-9


def test_get_stats_with_unlabeled_rows() -> None:
    """Mix of labeled and unlabeled rows; accuracy computed only over labeled."""
    labeled = [_row(llm_label="correct", label_verdict="correct") for _ in range(2)]
    unlabeled = [_row() for _ in range(3)]
    app = _build_app(_StubSession(rows=labeled + unlabeled))
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/architect-gold/stats")
    body = resp.json()
    assert body["total"] == 5
    assert body["labeled_total"] == 2
    assert body["unlabeled"] == 3
    assert body["label_accuracy"] == 1.0


# ── Auto-discovery contract ───────────────────────────────────────────────────


def test_aggregator_router_is_prefixed_and_tagged() -> None:
    """Review aggregator is under ``/api/v1/review`` with tag ``review``."""
    assert review_pkg.router.prefix == "/api/v1/review"
    assert "review" in review_pkg.router.tags


def test_review_architect_gold_is_auto_discovered() -> None:
    """Auto-discovery picks up ``review_architect_gold.py`` without editing ``__init__.py``."""
    assert "review_architect_gold" in review_pkg.MOUNTED_MODULES
    paths = {getattr(route, "path", None) for route in review_pkg.router.routes}
    assert "/api/v1/review/architect-gold/next" in paths
    assert "/api/v1/review/architect-gold/stats" in paths


def test_app_mounts_review_router_once() -> None:
    """``app.py`` includes the review aggregator exactly once."""
    from treadmill_api.app import create_app

    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/v1/review/architect-gold/next" in paths
    assert "/api/v1/review/architect-gold/stats" in paths


def test_init_does_not_enumerate_sibling_module_names() -> None:
    """Auto-discovery contract: ``__init__.py`` must not name siblings."""
    init_path = Path(review_pkg.__file__)
    source = init_path.read_text()
    assert "review_architect_gold" not in source, (
        "routers/review/__init__.py mentions 'review_architect_gold' by name — "
        "auto-discovery should not enumerate siblings"
    )


def test_review_architect_gold_module_exposes_router_attribute() -> None:
    """Sibling contract: the module exports a top-level ``router``."""
    assert isinstance(ag_mod.router, APIRouter)


def test_discovery_picks_up_a_freshly_added_sibling() -> None:
    """Re-run discovery against a synthetic sibling — no ``__init__.py`` edit needed.

    Mirrors ``test_routers_dashboard_init.py``'s synthetic-sibling test.
    """
    pkg_dir = Path(review_pkg.__file__).parent
    sibling_path = pkg_dir / "_test_synthetic_review_sibling.py"
    sibling_path.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/_synthetic-review-probe')\n"
        "async def _probe() -> dict:\n"
        "    return {'ok': True}\n"
    )
    try:
        sys.modules.pop(
            "treadmill_api.routers.review._test_synthetic_review_sibling",
            None,
        )
        fresh_aggregator = APIRouter(
            prefix=review_pkg.router.prefix,
            tags=list(review_pkg.router.tags),
        )
        mounted = review_pkg._discover_and_mount(fresh_aggregator)

        assert "_test_synthetic_review_sibling" in mounted
        app = FastAPI()
        app.include_router(fresh_aggregator)
        paths = {getattr(route, "path", None) for route in app.routes}
        assert "/api/v1/review/_synthetic-review-probe" in paths
    finally:
        sibling_path.unlink(missing_ok=True)
        sys.modules.pop(
            "treadmill_api.routers.review._test_synthetic_review_sibling",
            None,
        )
        importlib.reload(review_pkg)
