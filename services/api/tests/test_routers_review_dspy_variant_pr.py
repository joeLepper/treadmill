"""Unit tests for ``/api/v1/review/dspy-variant-pr`` (ADR-0070 substep 4.2).

Exercises the four route handlers with a stub async session — no live
database.  ``ReviewDspyVariantPrStore`` is patched at the module seam so
the SQL round-trip is replaced by a mock asserting kwarg shape.  Mirrors
``test_routers_triage_labels.py`` and ``test_routers_review_base.py`` in
style: hermetic, fast, pins the HTTP contract.

SQL-correctness assertions (ORDER BY confidence, partial-index WHERE,
COUNT math) live in the sibling integration file gated by
``TREADMILL_INTEGRATION=1``; here the router's contract with the store
(call kwargs + HTTP response shape) is what matters.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers import review as review_pkg
from treadmill_api.routers.review import review_dspy_variant_pr as router_mod
from treadmill_api.services.review_stats import StatsResponse


# ── Stub session ──────────────────────────────────────────────────────────────


class _StubSession:
    """In-memory stand-in for an AsyncSession.

    Real SQL flows through the patched store, so ``execute`` here is a
    defensive no-op that raises if exercised — any direct execute() call
    is a regression of the store-seam contract.  ``commit`` and
    ``refresh`` are counter-tracked so tests can assert they fired.
    """

    def __init__(self) -> None:
        self.commits = 0
        self.refreshes = 0
        self.refresh_targets: list[Any] = []

    async def execute(self, _statement: Any) -> Any:  # pragma: no cover
        raise AssertionError(
            "stub session should not receive direct execute() calls — "
            "the router goes through ReviewDspyVariantPrStore (patched in tests)"
        )

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshes += 1
        self.refresh_targets.append(obj)


def _build_app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(review_pkg.router)

    def _override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    return app


# ── Row builder ───────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(
    *,
    row_id: uuid.UUID | None = None,
    llm_label: str = "merge",
    llm_confidence: str = "high",
    label_verdict: str | None = None,
    label_notes: str | None = None,
    label_override_reason: str | None = None,
    labeled_by: str | None = None,
    labeled_at: datetime | None = None,
    label_guidelines_version: str | None = None,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    """Minimal Row-like object exposing every ``ReviewDspyVariantPr`` field
    via ``from_attributes=True`` (Pydantic v2)."""
    rid = row_id or uuid.uuid4()
    return SimpleNamespace(
        # Provenance
        id=rid,
        created_at=created_at or _now(),
        source_run_id=uuid.uuid4(),
        source_pr_number=4321,
        source_pr_url="https://github.com/joeLepper/treadmill/pull/4321",
        # Candidate content
        judge_role="role-architect",
        judge_prompt_path="treadmill_api/starters/role_architect.md",
        current_score=0.7,
        variant_score=0.7543,
        improvement=0.0543,
        patch_diff="--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-old\n+new\n",
        corpus_s3_uri="s3://treadmill-personal/optimizer/runs/x/corpus.jsonl",
        # LLM recommendation
        llm_label=llm_label,
        llm_confidence=llm_confidence,
        llm_rationale="Higher score, no regressions in spot checks.",
        llm_prompt_version="v1.0.0",
        llm_model="claude-opus-4-7",
        # Operator label
        label_verdict=label_verdict,
        label_notes=label_notes,
        label_override_reason=label_override_reason,
        # Labeled metadata
        labeled_by=labeled_by,
        labeled_at=labeled_at,
        label_guidelines_version=label_guidelines_version,
        # Outcome
        outcome_state=None,
        outcome_merged_at=None,
    )


def _patch_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    get_next_rows: list[SimpleNamespace] | None = None,
    get_by_id_row: SimpleNamespace | None = None,
    stats_response: StatsResponse | None = None,
    record_label_side_effect: Any = None,
) -> SimpleNamespace:
    """Replace ``ReviewDspyVariantPrStore`` with a factory returning a
    SimpleNamespace of AsyncMocks the test asserts on.
    """
    get_next = AsyncMock(return_value=list(get_next_rows or []))
    get_by_id = AsyncMock(return_value=get_by_id_row)
    record_label = AsyncMock(
        return_value=None, side_effect=record_label_side_effect,
    )
    stats = AsyncMock(
        return_value=stats_response
        or StatsResponse(
            total=0,
            unlabeled=0,
            labeled_total=0,
            label_accuracy=None,
            accuracy_last_100=None,
        )
    )
    fake = SimpleNamespace(
        get_next=get_next,
        get_by_id=get_by_id,
        record_label=record_label,
        stats=stats,
    )
    monkeypatch.setattr(router_mod, "ReviewDspyVariantPrStore", lambda: fake)
    return fake


# ── GET /next ─────────────────────────────────────────────────────────────────


def test_get_next_returns_unlabeled_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /next returns whatever ``store.get_next`` produced — the unit
    test verifies the HTTP shape; the store-layer test pins the SQL
    ``WHERE label_verdict IS NULL`` filter.
    """
    row_a = _row()
    row_b = _row()
    fake = _patch_store(monkeypatch, get_next_rows=[row_a, row_b])

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/dspy-variant-pr/next")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    ids = {item["id"] for item in body}
    assert ids == {str(row_a.id), str(row_b.id)}
    fake.get_next.assert_awaited_once()
    _, kwargs = fake.get_next.await_args
    assert kwargs == {"limit": 50}  # default


def test_get_next_orders_low_confidence_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router faithfully forwards the store's row order to the
    client (low → medium → high).  The store's CASE-expression ORDER BY
    is what produces this order in the real DB; the integration sibling
    test covers the SQL semantics directly.
    """
    row_low = _row(llm_confidence="low")
    row_med = _row(llm_confidence="medium")
    row_high = _row(llm_confidence="high")
    fake = _patch_store(
        monkeypatch, get_next_rows=[row_low, row_med, row_high],
    )

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/dspy-variant-pr/next")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["llm_confidence"] for item in body] == ["low", "medium", "high"]
    fake.get_next.assert_awaited_once()


def test_get_next_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``?limit=2`` is forwarded as ``limit=2`` to ``store.get_next``."""
    rows = [_row() for _ in range(2)]
    fake = _patch_store(monkeypatch, get_next_rows=rows)

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/dspy-variant-pr/next?limit=2")

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2
    fake.get_next.assert_awaited_once()
    _, kwargs = fake.get_next.await_args
    assert kwargs == {"limit": 2}


# ── GET /{id} ─────────────────────────────────────────────────────────────────


def test_get_by_id_404_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A random UUID with no matching row returns 404."""
    _patch_store(monkeypatch, get_by_id_row=None)
    missing_id = uuid.uuid4()
    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.get(f"/api/v1/review/dspy-variant-pr/{missing_id}")
    assert resp.status_code == 404
    assert str(missing_id) in resp.json()["detail"]


def test_get_by_id_returns_full_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every field across the six ADR-0070 layers round-trips through GET."""
    row = _row(
        label_verdict="merge",
        labeled_by="operator",
        labeled_at=_now(),
        label_guidelines_version="v1",
    )
    fake = _patch_store(monkeypatch, get_by_id_row=row)

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.get(f"/api/v1/review/dspy-variant-pr/{row.id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Provenance
    assert body["id"] == str(row.id)
    assert body["source_run_id"] == str(row.source_run_id)
    assert body["source_pr_number"] == 4321
    assert body["source_pr_url"].endswith("/4321")
    # Candidate content
    assert body["judge_role"] == "role-architect"
    assert body["judge_prompt_path"].endswith("role_architect.md")
    assert body["current_score"] == 0.7
    assert body["variant_score"] == 0.7543
    assert body["improvement"] == 0.0543
    assert "+new" in body["patch_diff"]
    assert body["corpus_s3_uri"].startswith("s3://")
    # LLM recommendation
    assert body["llm_label"] == "merge"
    assert body["llm_confidence"] == "high"
    assert body["llm_rationale"].startswith("Higher score")
    assert body["llm_prompt_version"] == "v1.0.0"
    assert body["llm_model"] == "claude-opus-4-7"
    # Operator label
    assert body["label_verdict"] == "merge"
    # Labeled metadata
    assert body["labeled_by"] == "operator"
    assert body["labeled_at"] is not None
    assert body["label_guidelines_version"] == "v1"
    # Outcome
    assert body["outcome_state"] is None
    assert body["outcome_merged_at"] is None

    fake.get_by_id.assert_awaited_once()
    args, _ = fake.get_by_id.await_args
    # Signature: (session, row_id)
    assert args[1] == row.id


# ── POST /{id}/label ──────────────────────────────────────────────────────────


def test_post_label_persists_and_stamps_labeled_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /label calls ``store.record_label`` with the body's kwargs and
    the response carries a stamped ``labeled_at`` + ``label_guidelines_version``.

    The unit test asserts the kwarg-shape contract; the store-layer
    integration test asserts that the persisted row actually carries
    ``func.now()`` + ``"v1"`` after the round-trip.
    """
    row = _row(llm_label="merge")

    async def _stamp(_session: Any, _row_id: uuid.UUID, **_kwargs: Any) -> None:
        # Simulate the DB-side stamp the store would produce.
        row.label_verdict = _kwargs["label_verdict"]
        row.label_notes = _kwargs["label_notes"]
        row.label_override_reason = _kwargs["label_override_reason"]
        row.labeled_by = _kwargs["labeled_by"]
        row.labeled_at = _now()
        row.label_guidelines_version = "v1"

    fake = _patch_store(
        monkeypatch,
        get_by_id_row=row,
        record_label_side_effect=_stamp,
    )

    session = _StubSession()
    app = _build_app(session)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/dspy-variant-pr/{row.id}/label",
            json={
                "label_verdict": "merge",
                "label_notes": "LGTM",
                "labeled_by": "operator",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["label_verdict"] == "merge"
    assert body["label_notes"] == "LGTM"
    assert body["labeled_by"] == "operator"
    assert body["labeled_at"] is not None
    assert body["label_guidelines_version"] == "v1"

    fake.record_label.assert_awaited_once()
    args, kwargs = fake.record_label.await_args
    # Signature: record_label(session, row_id, **fields)
    assert args[1] == row.id
    assert kwargs == {
        "label_verdict": "merge",
        "label_notes": "LGTM",
        "label_override_reason": None,
        "labeled_by": "operator",
    }
    assert session.commits == 1
    assert session.refreshes == 1


def test_post_label_404_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown row_id → 404, store.record_label is never called."""
    fake = _patch_store(monkeypatch, get_by_id_row=None)
    missing_id = uuid.uuid4()
    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/dspy-variant-pr/{missing_id}/label",
            json={"label_verdict": "merge", "labeled_by": "operator"},
        )
    assert resp.status_code == 404
    fake.record_label.assert_not_awaited()


def test_post_label_requires_override_reason_when_disagreeing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator verdict ≠ LLM recommendation + no ``override_reason`` → 422."""
    row = _row(llm_label="merge")
    fake = _patch_store(monkeypatch, get_by_id_row=row)

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/dspy-variant-pr/{row.id}/label",
            json={"label_verdict": "drop", "labeled_by": "operator"},
        )
    assert resp.status_code == 422
    assert "override_reason" in resp.json()["detail"]
    fake.record_label.assert_not_awaited()


def test_post_label_accepts_agreement_without_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verdict matches LLM → no override_reason needed → 200."""
    row = _row(llm_label="merge")

    async def _stamp(_session: Any, _row_id: uuid.UUID, **kwargs: Any) -> None:
        row.label_verdict = kwargs["label_verdict"]
        row.labeled_by = kwargs["labeled_by"]
        row.labeled_at = _now()
        row.label_guidelines_version = "v1"

    fake = _patch_store(
        monkeypatch, get_by_id_row=row, record_label_side_effect=_stamp,
    )

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/dspy-variant-pr/{row.id}/label",
            json={"label_verdict": "merge", "labeled_by": "operator"},
        )
    assert resp.status_code == 200, resp.text
    fake.record_label.assert_awaited_once()


def test_post_label_invalid_verdict_rejected_by_pydantic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verdict outside the closed enum → 422 from Pydantic (Literal)."""
    row = _row()
    fake = _patch_store(monkeypatch, get_by_id_row=row)

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/review/dspy-variant-pr/{row.id}/label",
            json={"label_verdict": "bogus", "labeled_by": "operator"},
        )
    assert resp.status_code == 422
    fake.record_label.assert_not_awaited()


# ── GET /stats ────────────────────────────────────────────────────────────────


def test_stats_returns_zero_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty table → zero counts + null accuracies."""
    fake = _patch_store(
        monkeypatch,
        stats_response=StatsResponse(
            total=0,
            unlabeled=0,
            labeled_total=0,
            label_accuracy=None,
            accuracy_last_100=None,
        ),
    )

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/dspy-variant-pr/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "total": 0,
        "unlabeled": 0,
        "labeled_total": 0,
        "label_accuracy": None,
        "accuracy_last_100": None,
    }
    fake.stats.assert_awaited_once()


def test_stats_label_accuracy_math(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4 labeled rows (3 agree with llm_label, 1 disagrees) → 0.75."""
    # The store would produce this StatsResponse from a real DB; here we
    # canned it.  The integration sibling test verifies the SQL math.
    fake = _patch_store(
        monkeypatch,
        stats_response=StatsResponse(
            total=4,
            unlabeled=0,
            labeled_total=4,
            label_accuracy=0.75,
            accuracy_last_100=None,
        ),
    )

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        resp = client.get("/api/v1/review/dspy-variant-pr/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 4
    assert body["labeled_total"] == 4
    assert abs(body["label_accuracy"] - 0.75) < 1e-9
    assert body["accuracy_last_100"] is None  # < 100 labeled
    fake.stats.assert_awaited_once()


# ── Auto-discovery contract ───────────────────────────────────────────────────


def test_router_auto_mounted_under_review() -> None:
    """Discovery picks up ``review_dspy_variant_pr.py`` without any edit
    to ``routers/review/__init__.py`` — mirrors the dashboard contract
    in ``test_routers_dashboard_init.py:41-47``.
    """
    assert "review_dspy_variant_pr" in review_pkg.MOUNTED_MODULES
    paths = {getattr(r, "path", None) for r in review_pkg.router.routes}
    assert "/api/v1/review/dspy-variant-pr/next" in paths
    assert "/api/v1/review/dspy-variant-pr/stats" in paths
    assert "/api/v1/review/dspy-variant-pr/{row_id}" in paths
    assert "/api/v1/review/dspy-variant-pr/{row_id}/label" in paths


def test_module_exposes_module_level_router_attribute() -> None:
    """Discovery contract: this module must export ``router`` so the
    auto-discovery loop in ``routers/review/__init__.py`` mounts it.
    """
    from fastapi import APIRouter

    assert isinstance(router_mod.router, APIRouter)
    assert router_mod.router.prefix == "/dspy-variant-pr"


def test_label_guidelines_version_constant_is_v1() -> None:
    """The module-level constant is pinned to ``"v1"`` — bumping it is the
    signal that the labeling rubric has changed (ADR-0070 §Labeled metadata).
    """
    assert router_mod.LABEL_GUIDELINES_VERSION == "v1"
