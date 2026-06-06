"""Tests for the corpus exporters (ADR-0070 substep 3 task 4).

Behavioural coverage:

  * ``export_architect_gold`` reads labeled ArchitectGoldRow rows and
    writes one JSONL line per row with the FLAT shape the judge's
    ``_compose_example_prompt`` expects (top-level keys + top-level
    ``gold_verdict``, NO ``input`` wrapper).
  * Unlabeled rows (``label_verdict IS NULL``) are excluded by the
    query.
  * The validator-gold exporter writes the FLAT validator shape
    (``validation_id`` = source_step_id, ``verdict_emitted``,
    ``script_excerpt``, ``artifact_excerpt``, ``gold_verdict``).
  * Both exporters return the row count and create the parent
    directory if missing.

Pure unit tests with a mocked AsyncSession returning canned rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.corpus_export import (
    export_architect_gold,
    export_validator_gold,
)


def _architect_row(
    *,
    decision_id: str = "dec-1",
    verdict_emitted: str = "amend",
    rationale_excerpt: str = "needs fixes",
    gate_log_uri: str | None = "s3://logs/gate-1",
    label_verdict: str | None = "correct",
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.decision_id = decision_id
    row.verdict_emitted = verdict_emitted
    row.rationale_excerpt = rationale_excerpt
    row.gate_log_uri = gate_log_uri
    row.label_verdict = label_verdict
    row.created_at = datetime.now(timezone.utc)
    return row


def _validator_row(
    *,
    source_step_id: uuid.UUID | None = None,
    verdict_emitted: str = "pass",
    script_excerpt: str = "uv run pytest -q",
    artifact_excerpt: str = "all green",
    label_verdict: str | None = "correct-verdict",
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.source_step_id = source_step_id or uuid.uuid4()
    row.verdict_emitted = verdict_emitted
    row.script_excerpt = script_excerpt
    row.artifact_excerpt = artifact_excerpt
    row.label_verdict = label_verdict
    row.created_at = datetime.now(timezone.utc)
    return row


def _mock_session_returning(rows: list[Any]) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = rows
    result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=result)
    return session


# ── architect-gold ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_architect_gold_writes_flat_jsonl(tmp_path: Path) -> None:
    rows = [
        _architect_row(
            decision_id="d1",
            verdict_emitted="amend",
            rationale_excerpt="r1",
            gate_log_uri="s3://logs/1",
            label_verdict="correct",
        ),
        _architect_row(
            decision_id="d2",
            verdict_emitted="accept-as-is",
            rationale_excerpt="r2",
            gate_log_uri=None,
            label_verdict="too-permissive",
        ),
    ]
    session = _mock_session_returning(rows)
    out = tmp_path / "architect-gold.jsonl"

    count = await export_architect_gold(session, out)

    assert count == 2
    lines = out.read_text("utf-8").splitlines()
    assert len(lines) == 2
    one = json.loads(lines[0])
    # FLAT shape — top-level keys, no ``input`` wrapper.
    assert "input" not in one
    assert one["decision_id"] == "d1"
    assert one["verdict_emitted"] == "amend"
    assert one["rationale_excerpt"] == "r1"
    assert one["gate_log_uri"] == "s3://logs/1"
    assert one["gold_verdict"] == "correct"
    assert one["example_id"]  # uuid populated
    two = json.loads(lines[1])
    assert two["gate_log_uri"] is None
    assert two["gold_verdict"] == "too-permissive"


@pytest.mark.asyncio
async def test_export_architect_gold_creates_parent_dir(tmp_path: Path) -> None:
    session = _mock_session_returning([])
    out = tmp_path / "nested" / "deep" / "out.jsonl"

    count = await export_architect_gold(session, out)

    assert count == 0
    assert out.exists()
    assert out.parent.exists()


@pytest.mark.asyncio
async def test_export_architect_gold_empty_result_returns_zero(
    tmp_path: Path,
) -> None:
    session = _mock_session_returning([])
    out = tmp_path / "empty.jsonl"
    count = await export_architect_gold(session, out)
    assert count == 0
    assert out.read_text("utf-8") == ""


# ── validator-gold ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_validator_gold_writes_flat_jsonl(tmp_path: Path) -> None:
    step_id = uuid.uuid4()
    rows = [
        _validator_row(
            source_step_id=step_id,
            verdict_emitted="pass",
            script_excerpt="uv run pytest -q tests/test_x.py",
            artifact_excerpt="3 passed",
            label_verdict="correct-verdict",
        ),
        _validator_row(
            verdict_emitted="fail",
            script_excerpt="make lint",
            artifact_excerpt="2 errors",
            label_verdict="wrong-verdict",
        ),
    ]
    session = _mock_session_returning(rows)
    out = tmp_path / "validator-gold.jsonl"

    count = await export_validator_gold(session, out)

    assert count == 2
    lines = out.read_text("utf-8").splitlines()
    assert len(lines) == 2
    one = json.loads(lines[0])
    assert "input" not in one
    assert one["validation_id"] == str(step_id)
    assert one["verdict_emitted"] == "pass"
    assert one["script_excerpt"] == "uv run pytest -q tests/test_x.py"
    assert one["artifact_excerpt"] == "3 passed"
    assert one["gold_verdict"] == "correct-verdict"
    two = json.loads(lines[1])
    assert two["verdict_emitted"] == "fail"
    assert two["gold_verdict"] == "wrong-verdict"


@pytest.mark.asyncio
async def test_export_validator_gold_creates_parent_dir(tmp_path: Path) -> None:
    session = _mock_session_returning([])
    out = tmp_path / "nested" / "v.jsonl"
    count = await export_validator_gold(session, out)
    assert count == 0
    assert out.exists()
