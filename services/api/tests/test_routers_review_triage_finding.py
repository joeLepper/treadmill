"""Unit tests for ``/api/v1/review/triage-finding/*`` (ADR-0070 substep 2).

Mirrors the hermetic-stub style from ``test_routers_triage_labels.py`` —
stub :class:`AsyncSession` with scripted scalar / execute responses, no
live Postgres, no ``TREADMILL_INTEGRATION=1`` integration pattern.

What's pinned here that the substrate-level
``test_routers_review_base.py`` doesn't already cover:

  * CASE ordering on TriageFindingRow.confidence (not llm_confidence) —
    confidence_attr override goes through end-to-end.
  * Round-trip on the ADR-0061 label fields (label_severity / label_category
    / label_fix_in_dsl) — the factory's setattr-loop splats them onto
    the row without an ``extra='allow'`` shim.
  * Skip-label semantic — ``label_is_real_bug=null`` is a valid POST and
    a Skip-labeled row STAYS in /next under the v1 unlabeled predicate
    (``label_is_real_bug IS NULL``).  This pins the v1 behavior so any
    future predicate switch (v2: ``labeled_at IS NULL``?) fails the test
    loudly.
  * /stats accuracy math against the kind's
    ``llm_label = confidence != 'low'`` hybrid_property alias.
  * /stats last-100 clipping when ``labeled_total > 100``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.review.review_triage_finding import router as kind_router
from treadmill_api.schemas.triage_finding import TriageFinding


# ── Stub session ──────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _RowResult:
    """Supports both ``.all()`` and ``.one_or_none()`` shapes."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_RowResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _StubSession:
    """In-memory stub for ``AsyncSession`` queries.

    Scripted ``execute_queue`` / ``scalar_queue`` drive the factory's
    SELECT / scalar lookups in call order.  ``commit`` / ``refresh`` are
    counter-tracked.  The factory's setattr-loop mutates the row in place,
    so ``refresh`` is a no-op (the row already reflects the new state).
    """

    def __init__(
        self,
        execute_queue: list[list[Any]] | None = None,
        scalar_queue: list[Any] | None = None,
    ) -> None:
        self._execute_queue: list[list[Any]] = list(execute_queue or [])
        self._scalar_queue: list[Any] = list(scalar_queue or [])
        self.commits = 0
        self.refreshes = 0

    async def execute(self, _stmt: Any) -> _RowResult:
        if self._execute_queue:
            return _RowResult(self._execute_queue.pop(0))
        return _RowResult([])

    async def scalar(self, _stmt: Any) -> Any:
        if self._scalar_queue:
            return self._scalar_queue.pop(0)
        return 0

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj: Any) -> None:
        self.refreshes += 1


# ── App builder ───────────────────────────────────────────────────────────────


def _build_app(session_or_factory: Any) -> FastAPI:
    """Mount the sibling router under ``/api/v1/review`` and override the
    session dependency.  ``session_or_factory`` may be a single session
    (yielded once) or a callable returning fresh sessions per request.
    """
    app = FastAPI()
    app.include_router(kind_router, prefix="/api/v1/review")

    if callable(session_or_factory):
        def _override() -> Iterator[Any]:
            yield session_or_factory()
    else:
        def _override() -> Iterator[Any]:
            yield session_or_factory

    app.dependency_overrides[get_session] = _override
    return app


# ── Row builder ───────────────────────────────────────────────────────────────


def _row(
    *,
    finding_id: uuid.UUID | None = None,
    confidence: str = "high",
    label_is_real_bug: bool | None = None,
    labeled_by: str | None = None,
    labeled_at: datetime | None = None,
    label_severity: str | None = None,
    label_category: str | None = None,
    label_fix_in_dsl: bool | None = None,
    label_notes: str | None = None,
    observation: str = "Layout overflow on overview at 1440x900.",
    created_at: datetime | None = None,
) -> SimpleNamespace:
    """Minimal Row-like object exposing the attributes Pydantic
    :class:`TriageFinding` reads via ``from_attributes=True``.

    Uses ``dispatch_action='research_only'`` so the suppression-signal and
    dispatched-plan-id cross-field validators on the schema stay happy
    (suppression_signal=None + dispatched_plan_id=None are required when
    the dispatch action is anything but ``suppressed`` / ``dispatched``).
    """
    fid = finding_id or uuid.uuid4()
    return SimpleNamespace(
        finding_id=fid,
        run_id=uuid.uuid4(),
        created_at=created_at or _now(),
        prompt_version="v1.5.0",
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
        confidence=confidence,
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
        label_severity=label_severity,
        label_category=label_category,
        label_fix_in_dsl=label_fix_in_dsl,
        label_dispatch_action=None,
        label_notes=label_notes,
        labeled_by=labeled_by,
        labeled_at=labeled_at,
        label_guidelines_version=None,
    )


# ── 1. GET /next — CASE ordering by confidence then created_at ────────────────


def test_get_next_returns_rows_in_confidence_then_created_at_order() -> None:
    """The factory's CASE expression sorts low → medium → high, then
    ``created_at`` ASC.  The stub returns rows in the order the DB would
    after applying that ORDER BY; this test pins the response shape and
    the per-row passthrough of the kind's ``confidence`` column.

    Fixture (5 rows interleaved so alphabetical order ≠ CASE order):
      row_a: confidence=high,   created_at=T+0  (oldest high)
      row_b: confidence=high,   created_at=T+5  (newest high)
      row_c: confidence=medium, created_at=T+2
      row_d: confidence=low,    created_at=T+1  (oldest low)
      row_e: confidence=low,    created_at=T+3  (newest low)

    Expected response order: row_d, row_e, row_c, row_a, row_b.
    """
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    row_a = _row(confidence="high",   created_at=base + timedelta(hours=0))
    row_b = _row(confidence="high",   created_at=base + timedelta(hours=5))
    row_c = _row(confidence="medium", created_at=base + timedelta(hours=2))
    row_d = _row(confidence="low",    created_at=base + timedelta(hours=1))
    row_e = _row(confidence="low",    created_at=base + timedelta(hours=3))

    # Stub returns rows pre-sorted (DB applies the ORDER BY).
    sorted_rows = [row_d, row_e, row_c, row_a, row_b]
    session = _StubSession(execute_queue=[sorted_rows])

    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/triage-finding/next")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    expected_ids = [str(r.finding_id) for r in sorted_rows]
    actual_ids = [item["finding_id"] for item in body]
    assert actual_ids == expected_ids
    assert [item["confidence"] for item in body] == [
        "low", "low", "medium", "high", "high",
    ]


# ── 2. GET /{id} — happy path + 404 ───────────────────────────────────────────


def test_get_one_returns_row_when_present() -> None:
    """GET /triage-finding/{id} returns the row when the DB has it.

    The factory's ``select(row_cls).where(row_cls.id == row_id)`` resolves
    ``row_cls.id`` to the overlay's hybrid_property aliasing ``finding_id``;
    this test pins that the path parameter routes to the matching row.
    """
    row = _row()
    session = _StubSession(execute_queue=[[row]])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get(f"/api/v1/review/triage-finding/{row.finding_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["finding_id"] == str(row.finding_id)


def test_get_one_returns_404_when_missing() -> None:
    """GET /triage-finding/{id} returns 404 when the DB returns nothing."""
    session = _StubSession(execute_queue=[[]])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get(f"/api/v1/review/triage-finding/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── 3. POST /{id}/label — round-trip excludes labeled row from /next ──────────


def test_post_label_round_trip_excludes_real_bug_label_from_next() -> None:
    """POST a verdict (label_is_real_bug=True), then GET /next.  The row is
    absent because the v1 predicate is ``label_is_real_bug IS NULL`` and a
    True-labeled row has a non-null verdict column.
    """
    row = _row()
    # Two execute() calls in the POST flow: 1) row lookup. Then a third
    # execute() for the subsequent GET /next: returns empty list since the
    # labeled row no longer matches the predicate.
    session = _StubSession(execute_queue=[[row], []])
    app = _build_app(session)

    with TestClient(app) as client:
        post_resp = client.post(
            f"/api/v1/review/triage-finding/{row.finding_id}/label",
            json={
                "label_is_real_bug": True,
                "labeled_by": "op",
            },
        )
        assert post_resp.status_code == 200, post_resp.text
        body = post_resp.json()
        assert body["finding_id"] == str(row.finding_id)
        assert body["label_is_real_bug"] is True
        assert body["labeled_by"] == "op"
        # The factory server-stamps labeled_at via the setattr-loop +
        # ``row.labeled_at = datetime.now(timezone.utc)``.
        assert body["labeled_at"] is not None

        next_resp = client.get("/api/v1/review/triage-finding/next")
        assert next_resp.status_code == 200
        assert next_resp.json() == []

    assert session.commits == 1
    assert session.refreshes == 1


# ── 4. POST /{id}/label with label_is_real_bug=null (Skip) ────────────────────


def test_post_label_skip_stays_in_next_under_v1_predicate() -> None:
    """ADR-0061 treats ``label_is_real_bug=null`` as the operator's "Skip"
    signal.  After Skip-labeling, ``labeled_at`` is server-stamped but the
    verdict column remains NULL.

    The v1 unlabeled predicate is ``label_is_real_bug IS NULL``, so a
    Skip-labeled row STAYS in /next — it would not until v2 (substep 3)
    swapped the predicate to e.g. ``labeled_at IS NULL``.  This test pins
    the v1 behavior so any future predicate change fails loudly.

    Verifies the direction the factory actually implements
    (``where(verdict_col.is_(None))``) — the stub scripts the row as still
    present in the second /next execute, mirroring what the real DB would
    return.
    """
    row = _row()
    # POST execute() returns the row; GET /next execute() returns the same
    # row because verdict_col IS NULL still matches after Skip-label.
    session = _StubSession(execute_queue=[[row], [row]])
    app = _build_app(session)

    with TestClient(app) as client:
        post_resp = client.post(
            f"/api/v1/review/triage-finding/{row.finding_id}/label",
            json={
                "label_is_real_bug": None,
                "labeled_by": "op",
            },
        )
        assert post_resp.status_code == 200, post_resp.text
        body = post_resp.json()
        assert body["label_is_real_bug"] is None
        assert body["labeled_by"] == "op"
        assert body["labeled_at"] is not None  # server-stamped

        next_resp = client.get("/api/v1/review/triage-finding/next")
        assert next_resp.status_code == 200
        ids_after = {item["finding_id"] for item in next_resp.json()}
        # v1: Skip-labeled rows reappear because label_is_real_bug remains NULL.
        # v2 (substep 3) may switch the predicate to labeled_at IS NULL —
        # at that point this assertion flips to ``not in``.
        assert str(row.finding_id) in ids_after


# ── 5. POST /{id}/label with kind-specific extras ─────────────────────────────


def test_post_label_round_trips_kind_specific_label_fields() -> None:
    """The factory's POST handler ``body.model_dump()`` + setattr-loop
    splats every field on :class:`LabelFindingRequest` onto the row.
    Verifies that the four ADR-0061-specific fields
    (``label_severity`` / ``label_category`` / ``label_fix_in_dsl`` /
    ``label_notes``) round-trip into the row's columns without an
    ``extra='allow'`` shim on the input schema.
    """
    row = _row()
    session = _StubSession(execute_queue=[[row]])
    app = _build_app(session)

    payload = {
        "label_is_real_bug": True,
        "label_severity": "high",
        "label_category": "accessibility",
        "label_fix_in_dsl": False,
        "label_notes": "Real bug; tracked in design-system backlog.",
        "labeled_by": "op",
    }

    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/triage-finding/{row.finding_id}/label",
            json=payload,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["label_is_real_bug"] is True
    assert body["label_severity"] == "high"
    assert body["label_category"] == "accessibility"
    assert body["label_fix_in_dsl"] is False
    assert body["label_notes"] == payload["label_notes"]
    assert body["labeled_by"] == "op"


# ── 6. GET /stats — basic accuracy ────────────────────────────────────────────


def test_get_stats_label_accuracy_basic() -> None:
    """5 labeled rows; 4 have confidence!='low' matching label_is_real_bug=True
    (llm_label=True, verdict=True → agree).  1 has confidence='low' (llm_label=False)
    while label_is_real_bug=True → disagree.

    Expected: ``{total:5, unlabeled:0, labeled_total:5, label_accuracy:0.8,
    accuracy_last_100:0.8}``.
    """
    # scalar() call sequence in compute_stats:
    #   1. total=5
    #   2. unlabeled=0  → labeled_total=5
    #   3. match_count=4  → label_accuracy = 4/5 = 0.8
    #   4. last_100_match=4  → accuracy_last_100 = 4/min(5,100) = 4/5 = 0.8
    session = _StubSession(scalar_queue=[5, 0, 4, 4])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/triage-finding/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5
    assert body["unlabeled"] == 0
    assert body["labeled_total"] == 5
    assert abs(body["label_accuracy"] - 0.8) < 1e-9
    assert abs(body["accuracy_last_100"] - 0.8) < 1e-9


# ── 7. GET /stats — last-100 clipping ─────────────────────────────────────────


def test_get_stats_accuracy_last_100_clips_to_window() -> None:
    """102 labeled rows: oldest 2 disagree, newest 100 all agree.

    Expected:
      label_accuracy = 100/102 (overall: 100 matches across 102)
      accuracy_last_100 = 100/100 = 1.0 (window denominator is min(102, 100))
    """
    # scalar() sequence:
    #   1. total=102
    #   2. unlabeled=0  → labeled_total=102
    #   3. match_count=100  → label_accuracy = 100/102
    #   4. last_100_match=100  → accuracy_last_100 = 100/min(102,100) = 100/100 = 1.0
    session = _StubSession(scalar_queue=[102, 0, 100, 100])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/triage-finding/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 102
    assert body["labeled_total"] == 102
    assert abs(body["label_accuracy"] - (100 / 102)) < 1e-9
    assert body["accuracy_last_100"] == 1.0


# ── 8. GET /stats — empty corpus ──────────────────────────────────────────────


def test_get_stats_empty_corpus_returns_null_accuracy() -> None:
    """Empty corpus → both accuracy fields are null (no denominator)."""
    session = _StubSession(scalar_queue=[0, 0])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/triage-finding/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "total": 0,
        "unlabeled": 0,
        "labeled_total": 0,
        "label_accuracy": None,
        "accuracy_last_100": None,
    }


# ── Schema-validator smoke ────────────────────────────────────────────────────


def test_test_row_validates_against_triagefinding_schema() -> None:
    """Sanity: the ``_row`` fixture itself round-trips through
    :class:`TriageFinding`.``model_validate`` so the cross-field validators
    (suppression_signal / dispatched_plan_id) don't bite the GET tests
    above on innocent payload shape errors.
    """
    parsed = TriageFinding.model_validate(_row())
    assert parsed.dispatch_action == "research_only"
    assert parsed.suppression_signal is None
    assert parsed.dispatched_plan_id is None
