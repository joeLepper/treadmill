"""Integration loop for triage-finding review queue (ADR-0070 substep 2 step 3).

Tests the full end-to-end path: GET /next (unlabeled), label all rows,
GET /stats (accuracy aggregation). Validates that the accuracy widget reads
the correct stats endpoint with the kind-aware path substitution.

Fixture distribution exercises both directions of LLM-vs-operator disagreement
per the ADR-0070 specification. The llm_label = (confidence != 'low') alias
means:
  - low confidence → llm_label=False (operator verdict must be False to match)
  - medium/high confidence → llm_label=True (operator verdict must be True to match)

Concrete fixture:
  row1: confidence=low,    label_is_real_bug=True  (mismatch: False != True)
  row2: confidence=low,    label_is_real_bug=False (match: False == False)
  row3: confidence=medium, label_is_real_bug=True  (match: True == True)
  row4: confidence=high,   label_is_real_bug=False (mismatch: True != False)
  row5: confidence=high,   label_is_real_bug=True  (match: True == True)

Expected baseline: 3 matches / 5 = 0.6 accuracy.
After mutating row4 to True: 4 matches / 5 = 0.8 accuracy.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.review.review_triage_finding import router as kind_router


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
    """In-memory stub for AsyncSession queries.

    Scripted execute_queue / scalar_queue drive the factory's SELECT / scalar
    lookups in call order. commit / refresh are counter-tracked.
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


def _build_app(session_factory: Any) -> FastAPI:
    """Mount the triage-finding router under /api/v1/review."""
    app = FastAPI()
    app.include_router(kind_router, prefix="/api/v1/review")

    def _override() -> Iterator[Any]:
        yield session_factory()

    app.dependency_overrides[get_session] = _override
    return app


def _row(
    *,
    finding_id: uuid.UUID | None = None,
    confidence: str = "high",
    label_is_real_bug: bool | None = None,
    labeled_by: str | None = None,
    labeled_at: datetime | None = None,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    """Minimal Row-like object for TriageFinding."""
    fid = finding_id or uuid.uuid4()
    return SimpleNamespace(
        finding_id=fid,
        run_id=uuid.uuid4(),
        created_at=created_at or datetime.now(timezone.utc),
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
        observation="Test observation.",
        evidence_pointer="screen.png:y=80-900",
        proposed_resolution="Test resolution.",
        dispatch_action="research_only",
        dispatch_reason="Test reason.",
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
        labeled_by=labeled_by,
        labeled_at=labeled_at,
        label_guidelines_version=None,
    )


def test_triage_finding_end_to_end_accuracy_loop() -> None:
    """Full integration loop: /next → label all 5 rows → /stats accuracy.

    Fixture distribution tests both directions of LLM-vs-operator disagreement:
      row1: low confidence, label=True (mismatch)
      row2: low confidence, label=False (match)
      row3: medium confidence, label=True (match)
      row4: high confidence, label=False (mismatch)
      row5: high confidence, label=True (match)

    Expected baseline: 3/5 = 0.6 accuracy.

    Then mutate row4.label from False → True and re-check: 4/5 = 0.8 accuracy.
    """
    base_time = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    # Create deterministic rows with explicit created_at timestamps.
    row1 = _row(
        confidence="low",
        label_is_real_bug=None,
        created_at=base_time,
    )
    row2 = _row(
        confidence="low",
        label_is_real_bug=None,
        created_at=datetime(2026, 6, 4, 12, 0, 1, tzinfo=timezone.utc),
    )
    row3 = _row(
        confidence="medium",
        label_is_real_bug=None,
        created_at=datetime(2026, 6, 4, 12, 0, 2, tzinfo=timezone.utc),
    )
    row4 = _row(
        confidence="high",
        label_is_real_bug=None,
        created_at=datetime(2026, 6, 4, 12, 0, 3, tzinfo=timezone.utc),
    )
    row5 = _row(
        confidence="high",
        label_is_real_bug=None,
        created_at=datetime(2026, 6, 4, 12, 0, 4, tzinfo=timezone.utc),
    )

    # Step 1: GET /next before labeling.
    # Stub returns rows in confidence-ascending order (low, low, medium, high, high)
    # with created_at ties broken by ascending timestamp.
    unlabeled_rows = [row1, row2, row3, row4, row5]

    # Step 2: POST /label × 5 (one for each row).
    # The factory's label_row handler mutates the row in place, so we mutate
    # them here to simulate the DB write.
    row1.label_is_real_bug = True    # mismatch: confidence=low → llm_label=False
    row2.label_is_real_bug = False   # match: confidence=low → llm_label=False
    row3.label_is_real_bug = True    # match: confidence=medium → llm_label=True
    row4.label_is_real_bug = False   # mismatch: confidence=high → llm_label=True
    row5.label_is_real_bug = True    # match: confidence=high → llm_label=True

    row1.labeled_by = "operator"
    row2.labeled_by = "operator"
    row3.labeled_by = "operator"
    row4.labeled_by = "operator"
    row5.labeled_by = "operator"

    labeled_rows_baseline = [row1, row2, row3, row4, row5]

    # Step 3: GET /stats — accuracy calculation.
    # scalar() calls in compute_stats order:
    #   1. total=5
    #   2. unlabeled=0 → labeled_total=5
    #   3. match_count=3 (row2, row3, row5) → label_accuracy = 3/5 = 0.6
    #   4. last_100_match=3 → accuracy_last_100 = 3/min(5,100) = 3/5 = 0.6

    session_factory = lambda: _StubSession(
        execute_queue=[
            unlabeled_rows,  # GET /next
            [row1],          # POST /{id}/label row1
            [row2],          # POST /{id}/label row2
            [row3],          # POST /{id}/label row3
            [row4],          # POST /{id}/label row4
            [row5],          # POST /{id}/label row5
        ],
        scalar_queue=[5, 0, 3, 3],  # GET /stats
    )

    app = _build_app(session_factory)

    with TestClient(app) as client:
        # GET /next before labeling
        next_resp = client.get("/api/v1/review/triage-finding/next?limit=5")
        assert next_resp.status_code == 200, next_resp.text
        next_body = next_resp.json()
        assert len(next_body) == 5
        # Verify order: confidence ascending (low, low, medium, high, high)
        confidences = [item["confidence"] for item in next_body]
        assert confidences == ["low", "low", "medium", "high", "high"]
        # Verify created_at order within confidence buckets
        ids_in_order = [item["finding_id"] for item in next_body]
        assert ids_in_order == [
            str(row1.finding_id),
            str(row2.finding_id),
            str(row3.finding_id),
            str(row4.finding_id),
            str(row5.finding_id),
        ]

        # POST /label × 5
        for row_data, label_value in [
            (row1, True),
            (row2, False),
            (row3, True),
            (row4, False),
            (row5, True),
        ]:
            post_resp = client.post(
                f"/api/v1/review/triage-finding/{row_data.finding_id}/label",
                json={
                    "label_is_real_bug": label_value,
                    "labeled_by": "operator",
                },
            )
            assert post_resp.status_code == 200, post_resp.text
            body = post_resp.json()
            assert body["label_is_real_bug"] == label_value
            assert body["labeled_by"] == "operator"

        # GET /stats — verify baseline accuracy
        stats_resp = client.get("/api/v1/review/triage-finding/stats")
        assert stats_resp.status_code == 200, stats_resp.text
        stats_body = stats_resp.json()
        assert stats_body["total"] == 5
        assert stats_body["unlabeled"] == 0
        assert stats_body["labeled_total"] == 5
        assert abs(stats_body["label_accuracy"] - 0.6) < 1e-9
        assert abs(stats_body["accuracy_last_100"] - 0.6) < 1e-9

    # Step 4: Mutate row4's label and re-test.
    # Flip row4 from False → True (now matches, since confidence=high → llm_label=True).
    row4.label_is_real_bug = True

    # Reset session with mutated row4
    session_factory = lambda: _StubSession(
        scalar_queue=[5, 0, 4, 4],  # total=5, unlabeled=0, match_count=4, last_100_match=4
    )
    app = _build_app(session_factory)

    with TestClient(app) as client:
        stats_resp = client.get("/api/v1/review/triage-finding/stats")
        assert stats_resp.status_code == 200, stats_resp.text
        stats_body = stats_resp.json()
        assert stats_body["total"] == 5
        assert stats_body["unlabeled"] == 0
        assert stats_body["labeled_total"] == 5
        assert abs(stats_body["label_accuracy"] - 0.8) < 1e-9
        assert abs(stats_body["accuracy_last_100"] - 0.8) < 1e-9
