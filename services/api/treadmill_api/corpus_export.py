"""Corpus exporters for ADR-0070 substep 3 task 4.

Materialize labeled architect-gold and validator-gold rows into JSONL
files the DSPy bot (Wave 4) consumes via ``judge_eval`` / its
``_compose_example_prompt``. Per that prompt-composer's contract the
JSONL is FLAT: top-level per-field keys + a top-level ``gold_verdict``.
Do NOT wrap fields in an ``input`` dict — that would render as one
``## input\\n<repr>`` section and silently mis-feed the judge.

Both exporters select rows whose ``label_verdict IS NOT NULL`` (i.e.
the operator labeled them via the dashboard's flip-through UI). The
``label_verdict`` becomes the FLAT top-level ``gold_verdict``.

The functions write local files only — S3 push happens via the
operator-facing ``tools/load-analysis-corpus.sh`` and is out of scope
for the worker sandbox.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models.architect_gold import ArchitectGoldRow
from treadmill_api.models.validator_gold import ValidatorGoldRow

logger = logging.getLogger("treadmill_api.corpus_export")


async def export_architect_gold(
    session: AsyncSession, out_path: Path,
) -> int:
    """Write every labeled architect-gold row to ``out_path`` as JSONL.

    Each line is a FLAT object:
        {
          "example_id": "<row.id>",
          "decision_id": "<row.decision_id>",
          "verdict_emitted": "<row.verdict_emitted>",
          "rationale_excerpt": "<row.rationale_excerpt>",
          "gate_log_uri": "<row.gate_log_uri or null>",
          "gold_verdict": "<row.label_verdict>"
        }

    Returns the count of rows written.
    """
    stmt = (
        select(ArchitectGoldRow)
        .where(ArchitectGoldRow.label_verdict.is_not(None))
        .order_by(ArchitectGoldRow.created_at)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            example = {
                "example_id": str(row.id),
                "decision_id": row.decision_id,
                "verdict_emitted": row.verdict_emitted,
                "rationale_excerpt": row.rationale_excerpt,
                "gate_log_uri": row.gate_log_uri,
                "gold_verdict": row.label_verdict,
            }
            fh.write(json.dumps(example) + "\n")
            written += 1

    logger.info(
        "exported %d architect-gold rows to %s", written, out_path,
    )
    return written


async def export_validator_gold(
    session: AsyncSession, out_path: Path,
) -> int:
    """Write every labeled validator-gold row to ``out_path`` as JSONL.

    Each line is a FLAT object:
        {
          "validation_id": "<row.source_step_id>",
          "verdict_emitted": "<row.verdict_emitted>",
          "script_excerpt": "<row.script_excerpt>",
          "artifact_excerpt": "<row.artifact_excerpt>",
          "gold_verdict": "<row.label_verdict>"
        }

    Returns the count of rows written.
    """
    stmt = (
        select(ValidatorGoldRow)
        .where(ValidatorGoldRow.label_verdict.is_not(None))
        .order_by(ValidatorGoldRow.created_at)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            example = {
                "validation_id": str(row.source_step_id),
                "verdict_emitted": row.verdict_emitted,
                "script_excerpt": row.script_excerpt,
                "artifact_excerpt": row.artifact_excerpt,
                "gold_verdict": row.label_verdict,
            }
            fh.write(json.dumps(example) + "\n")
            written += 1

    logger.info(
        "exported %d validator-gold rows to %s", written, out_path,
    )
    return written
