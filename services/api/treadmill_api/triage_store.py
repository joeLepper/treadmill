"""Async repository for ADR-0061 triage_findings.

Mirrors the pattern established in :mod:`treadmill_api.onboarding_store`:
every method takes an :class:`AsyncSession`; the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models.triage_finding import TriageFindingRow
from treadmill_api.schemas.triage_finding import TriageFinding


class TriageStore:
    """Async accessor for ``triage_findings``.

    All methods take an ``AsyncSession``; the caller commits.
    """

    async def insert_finding(
        self, session: AsyncSession, finding: TriageFinding
    ) -> uuid.UUID:
        """Persist a detector's finding and return its finding_id."""
        row = TriageFindingRow(
            finding_id=finding.finding_id,
            run_id=finding.run_id,
            prompt_version=finding.prompt_version,
            model=finding.model,
            mode=finding.mode,
            on_demand_request=finding.on_demand_request,
            target_url=finding.target_url,
            viewport_w=finding.viewport_w,
            viewport_h=finding.viewport_h,
            git_sha=finding.git_sha,
            api_git_sha=finding.api_git_sha,
            screenshot_uri=finding.screenshot_uri,
            viewport_png_uri=finding.viewport_png_uri,
            dom_snapshot_uri=finding.dom_snapshot_uri,
            console_log_uri=finding.console_log_uri,
            network_log_uri=finding.network_log_uri,
            evidence_summary=finding.evidence_summary,
            category=finding.category,
            severity=finding.severity,
            confidence=finding.confidence,
            observation=finding.observation,
            evidence_pointer=finding.evidence_pointer,
            proposed_resolution=finding.proposed_resolution,
            dispatch_action=finding.dispatch_action,
            dispatch_reason=finding.dispatch_reason,
            suppression_signal=finding.suppression_signal,
            parent_finding_id=finding.parent_finding_id,
            dispatched_plan_id=finding.dispatched_plan_id,
        )
        session.add(row)
        await session.flush()
        return row.finding_id

    async def update_outcome(
        self,
        session: AsyncSession,
        dispatched_plan_id: uuid.UUID,
        outcome_state: str,
        outcome_pr_number: int | None,
        outcome_merged_at: datetime | None,
    ) -> int:
        """Idempotent outcome projection driven by coordination-consumer events.

        Updates all findings whose ``dispatched_plan_id`` matches.
        Returns the number of rows updated (0 when the plan id is unknown).
        """
        result = await session.execute(
            sa.update(TriageFindingRow)
            .where(TriageFindingRow.dispatched_plan_id == dispatched_plan_id)
            .values(
                outcome_state=outcome_state,
                outcome_pr_number=outcome_pr_number,
                outcome_merged_at=outcome_merged_at,
            )
        )
        return result.rowcount  # type: ignore[return-value]

    async def record_label(
        self,
        session: AsyncSession,
        finding_id: uuid.UUID,
        *,
        label_is_real_bug: bool | None = None,
        label_severity: str | None = None,
        label_category: str | None = None,
        label_fix_in_dsl: bool | None = None,
        label_dispatch_action: str | None = None,
        label_notes: str | None = None,
        labeled_by: str | None = None,
        label_guidelines_version: str | None = None,
    ) -> None:
        """Persist operator labels for a finding."""
        await session.execute(
            sa.update(TriageFindingRow)
            .where(TriageFindingRow.finding_id == finding_id)
            .values(
                label_is_real_bug=label_is_real_bug,
                label_severity=label_severity,
                label_category=label_category,
                label_fix_in_dsl=label_fix_in_dsl,
                label_dispatch_action=label_dispatch_action,
                label_notes=label_notes,
                labeled_by=labeled_by,
                labeled_at=sa.func.now(),
                label_guidelines_version=label_guidelines_version,
            )
        )

    async def get_unlabeled_findings(
        self,
        session: AsyncSession,
        limit: int = 50,
    ) -> list[TriageFinding]:
        """Return up to ``limit`` findings whose ``label_is_real_bug`` is NULL.

        Uses the partial index ``ix_triage_findings_unlabeled`` so the
        labeling-UI "next unlabeled" query is constant-time regardless of
        corpus size.
        """
        rows = (
            await session.scalars(
                sa.select(TriageFindingRow)
                .where(TriageFindingRow.label_is_real_bug.is_(None))
                .order_by(TriageFindingRow.created_at.asc())
                .limit(limit)
            )
        ).all()
        return [TriageFinding.model_validate(row) for row in rows]
