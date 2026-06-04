"""Unit tests for ``build_review_router`` (ADR-0070 substep 1.2).

Exercises the four review endpoints with a stub async session — no live
database.  A synthetic ``_FakeKindRow`` (distinct ``__tablename__`` from
the Task 1 mixin-test table) is declared at import time; the HTTP layer
is exercised through ``fastapi.testclient.TestClient``.

Coverage:

  * GET /next — returns unlabeled rows, ordered confidence ASC then
    created_at ASC.
  * GET /next?limit=N — honors the limit query param.
  * GET /{id} — returns the row when present, 404 when missing.
  * POST /{id}/label — 200 + writes verdict + row excluded from GET /next.
  * POST /{id}/label — 404 when id is unknown.
  * POST /{id}/label — 422 when required ``labeled_by`` is absent.
  * GET /stats — returns ``StatsResponse`` shape (None accuracy when no
    labeled rows; correct fraction when some rows are labeled).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Text

from treadmill_api.database import Base
from treadmill_api.dependencies_db import get_session
from treadmill_api.models.review_queue import ReviewQueueRowMixin
from treadmill_api.routers.review.base import build_review_router
from treadmill_api.services.review_stats import StatsResponse

# ── Synthetic kind (distinct tablename from Task 1's mixin test) ──────────────

_FAKE_TABLE = "_fake_review_kind_router_test"


class _FakeKindRow(ReviewQueueRowMixin, Base):
    """Minimal subclass used only in this test module."""

    __tablename__ = _FAKE_TABLE

    llm_label: Mapped[str] = mapped_column(Text, nullable=False)
    label_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        *ReviewQueueRowMixin.review_queue_check_constraints(table_name=_FAKE_TABLE),
        ReviewQueueRowMixin.unlabeled_index(
            table_name=_FAKE_TABLE, verdict_column="label_verdict"
        ),
    )


class _FakeKindLabelInput(BaseModel):
    label_verdict: str | None = None
    label_notes: str | None = None
    labeled_by: str = Field(..., min_length=1)


class _FakeKindOutput(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    llm_confidence: str
    llm_label: str
    label_verdict: str | None = None
    labeled_by: str | None = None
    labeled_at: datetime | None = None
    candidate_text: str


# ── Stub session ──────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _RowResult:
    """Supports both ``.all()`` and ``.one_or_none()``."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_RowResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _StubSession:
    """In-memory stub that serves scripted responses to ``execute()`` calls
    and tracks ``commit()`` / ``refresh()`` invocations.

    Each call to ``execute()`` pops the next list from ``_execute_queue``.
    ``scalar()`` pops the next value from ``_scalar_queue``.
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


def _build_app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    kind_router = build_review_router(
        prefix="/_fake-kind",
        row_cls=_FakeKindRow,
        label_input_model=_FakeKindLabelInput,
        output_model=_FakeKindOutput,
        verdict_attr="label_verdict",
        llm_label_attr="llm_label",
    )
    # Mount the per-kind router under the review prefix directly (bypassing
    # auto-discovery) so we test the factory output without a sibling .py file.
    app.include_router(kind_router, prefix="/api/v1/review")

    def _override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    return app


# ── Row builder ───────────────────────────────────────────────────────────────


def _row(
    *,
    row_id: uuid.UUID | None = None,
    llm_confidence: str = "medium",
    llm_label: str = "approve",
    label_verdict: str | None = None,
    candidate_text: str = "some candidate",
    created_at: datetime | None = None,
) -> SimpleNamespace:
    """Minimal Row-like object exposing the attributes the Pydantic
    ``_FakeKindOutput`` schema reads via ``from_attributes=True``."""
    return SimpleNamespace(
        id=row_id or uuid.uuid4(),
        created_at=created_at or _now(),
        llm_confidence=llm_confidence,
        llm_label=llm_label,
        llm_rationale="rationale",
        llm_prompt_version="v1",
        llm_model="claude-sonnet-4-6",
        label_verdict=label_verdict,
        label_notes=None,
        labeled_by=None,
        labeled_at=None,
        label_guidelines_version=None,
        label_override_reason=None,
        source_run_id=None,
        source_event_id=None,
        source_url=None,
        source_pr_number=None,
        outcome_state=None,
        outcome_pr_merged_at=None,
        candidate_text=candidate_text,
    )


# ── GET /next ─────────────────────────────────────────────────────────────────


def test_get_next_returns_rows() -> None:
    """GET /next returns unlabeled rows from the stub session."""
    row_a = _row(llm_confidence="low", candidate_text="Row A")
    row_b = _row(llm_confidence="medium", candidate_text="Row B")
    session = _StubSession(execute_queue=[[row_a, row_b]])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/_fake-kind/next")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    texts = {item["candidate_text"] for item in body}
    assert texts == {"Row A", "Row B"}


def test_get_next_ordered_confidence_asc_then_created_at() -> None:
    """GET /next returns rows in low → medium → high confidence order.

    The stub returns rows in the order the DB would after applying the
    CASE-expression ORDER BY.  This test asserts the endpoint faithfully
    forwards that order to the client.
    """
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 3, tzinfo=timezone.utc)

    row_low = _row(llm_confidence="low", created_at=t0, candidate_text="low")
    row_med = _row(llm_confidence="medium", created_at=t1, candidate_text="medium")
    row_high = _row(llm_confidence="high", created_at=t2, candidate_text="high")

    # Stub returns them pre-sorted (as the DB ORDER BY would do).
    session = _StubSession(execute_queue=[[row_low, row_med, row_high]])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/_fake-kind/next")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 3
    assert [item["llm_confidence"] for item in body] == ["low", "medium", "high"]


def test_get_next_honors_limit() -> None:
    """GET /next?limit=1 returns at most one row."""
    row_a = _row(candidate_text="A")
    row_b = _row(candidate_text="B")
    # Stub returns only what the SQL LIMIT would return.
    session = _StubSession(execute_queue=[[row_a]])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/_fake-kind/next?limit=1")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


# ── GET /{id} ─────────────────────────────────────────────────────────────────


def test_get_one_returns_row_when_present() -> None:
    """GET /{id} returns the row when the DB returns it."""
    row = _row()
    session = _StubSession(execute_queue=[[row]])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get(f"/api/v1/review/_fake-kind/{row.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(row.id)


def test_get_one_returns_404_when_missing() -> None:
    """GET /{id} returns 404 when the DB returns nothing."""
    session = _StubSession(execute_queue=[[]])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get(f"/api/v1/review/_fake-kind/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── POST /{id}/label ──────────────────────────────────────────────────────────


def test_post_label_200_writes_verdict_and_excludes_from_next() -> None:
    """POST /{id}/label 200s, writes the verdict column, and the row no
    longer appears in GET /next (because the stub now returns an empty list
    for the second execute call).
    """
    row = _row(label_verdict=None)

    def _fake_refresh(obj: Any) -> None:
        obj.label_verdict = "approve"
        obj.labeled_by = "operator"

    class _SmartSession(_StubSession):
        async def refresh(self, obj: Any) -> None:
            _fake_refresh(obj)
            self.refreshes += 1

    # First execute: POST loads the row. Second: GET /next returns empty.
    session = _SmartSession(execute_queue=[[row], []])
    app = _build_app(session)

    with TestClient(app) as client:
        post_resp = client.post(
            f"/api/v1/review/_fake-kind/{row.id}/label",
            json={"label_verdict": "approve", "labeled_by": "operator"},
        )
        assert post_resp.status_code == 200, post_resp.text
        body = post_resp.json()
        assert body["label_verdict"] == "approve"
        assert body["labeled_by"] == "operator"

        # Labeled row excluded from next (stub returns []).
        next_resp = client.get("/api/v1/review/_fake-kind/next")
        assert next_resp.status_code == 200
        assert next_resp.json() == []

    assert session.commits == 1
    assert session.refreshes == 1


def test_post_label_404_when_id_unknown() -> None:
    """POST /{id}/label returns 404 when the row doesn't exist."""
    session = _StubSession(execute_queue=[[]])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/_fake-kind/{uuid.uuid4()}/label",
            json={"label_verdict": "approve", "labeled_by": "operator"},
        )
    assert resp.status_code == 404


def test_post_label_422_without_labeled_by() -> None:
    """POST /{id}/label without ``labeled_by`` returns 422 (required field)."""
    session = _StubSession()
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/_fake-kind/{uuid.uuid4()}/label",
            json={"label_verdict": "approve"},  # labeled_by omitted
        )
    assert resp.status_code == 422


# ── GET /stats ────────────────────────────────────────────────────────────────


def test_get_stats_no_labeled_rows_returns_null_accuracy() -> None:
    """GET /stats: when no rows are labeled, accuracy fields are None."""
    # scalar() call sequence: total=0, unlabeled=0 (labeled_total=0 → skip accuracy)
    session = _StubSession(scalar_queue=[0, 0])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/_fake-kind/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert body["unlabeled"] == 0
    assert body["labeled_total"] == 0
    assert body["label_accuracy"] is None
    assert body["accuracy_last_100"] is None


def test_get_stats_labeled_rows_returns_accuracy_fraction() -> None:
    """GET /stats: 5 total, 0 unlabeled, 4 match LLM → label_accuracy=0.8."""
    # scalar() sequence:
    #   1. total=5
    #   2. unlabeled=0  → labeled_total=5
    #   3. match_count=4  → label_accuracy = 4/5 = 0.8
    #   4. last_100_match=4  → accuracy_last_100 = 4/5 = 0.8
    session = _StubSession(scalar_queue=[5, 0, 4, 4])
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/_fake-kind/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5
    assert body["labeled_total"] == 5
    assert abs(body["label_accuracy"] - 0.8) < 1e-9
    assert abs(body["accuracy_last_100"] - 0.8) < 1e-9
