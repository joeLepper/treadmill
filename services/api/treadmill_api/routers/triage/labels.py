"""``GET /api/v1/triage/findings`` + ``POST .../label`` — ADR-0061 labeling UI.

Backs ``services/dashboard/src/api/queries.ts`` ``useUnlabeledFindings``
and ``useLabelFinding``. Wraps :class:`treadmill_api.triage_store.TriageStore`
— the persistence layer already exists; this module is the HTTP seam the
operator's flip-through labeling UI consumes.

The GET endpoint returns the unlabeled-finding queue for the operator to
walk through. The POST endpoint persists the four ADR-0061 label
dimensions (``is_real_bug`` / ``severity`` / ``category`` / ``fix_in_dsl``
plus a free-text ``notes``) — any of the four may be ``null`` because
null is a signal per the v1 prompt ("Skip means leave null").
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models.triage_finding import TriageFindingRow
from treadmill_api.schemas.triage_finding import (
    CategoryT,
    SeverityT,
    TriageFinding,
)
from treadmill_api.triage_store import TriageStore


router = APIRouter()


class LabelFindingRequest(BaseModel):
    """Operator-supplied labels for one finding.

    Per ADR-0061, ``null`` is a valid value for each of the four label
    dimensions — the v1 labeling prompt explicitly treats "Skip" as a
    signal (the labeler couldn't decide, didn't apply, etc.).
    ``labeled_by`` is the only non-null field — every label-write needs
    operator attribution for the corpus.
    """

    label_is_real_bug: bool | None = None
    label_severity: SeverityT | None = None
    label_category: CategoryT | None = None
    label_fix_in_dsl: bool | None = None
    label_notes: str | None = None
    labeled_by: str = Field(..., min_length=1)


@router.get("/findings", response_model=list[TriageFinding])
async def list_unlabeled_findings(
    session: Annotated[AsyncSession, Depends(get_session)],
    label_is_real_bug: Annotated[
        str | None,
        Query(
            description=(
                "Only ``null`` is supported today — the labeling UI walks "
                "the unlabeled queue. Future filters can extend this."
            ),
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[TriageFinding]:
    """Return up to ``limit`` unlabeled findings.

    The partial index ``ix_triage_findings_unlabeled`` keeps the lookup
    constant-time regardless of corpus size.
    """
    store = TriageStore()
    return await store.get_unlabeled_findings(session, limit=limit)


@router.post(
    "/findings/{finding_id}/label",
    response_model=TriageFinding,
    status_code=status.HTTP_200_OK,
)
async def label_finding(
    finding_id: uuid.UUID,
    body: LabelFindingRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TriageFinding:
    """Persist operator labels for ``finding_id`` and return the updated row.

    Returns 404 when ``finding_id`` doesn't exist. The four label fields
    accept ``null``; only ``labeled_by`` is required.
    """
    existing = (
        await session.scalars(
            select(TriageFindingRow).where(
                TriageFindingRow.finding_id == finding_id,
            )
        )
    ).one_or_none()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"finding {finding_id} not found",
        )

    store = TriageStore()
    await store.record_label(
        session,
        finding_id,
        label_is_real_bug=body.label_is_real_bug,
        label_severity=body.label_severity,
        label_category=body.label_category,
        label_fix_in_dsl=body.label_fix_in_dsl,
        label_notes=body.label_notes,
        labeled_by=body.labeled_by,
    )
    await session.commit()

    # Re-read so the response reflects the server-stamped ``labeled_at``
    # and any session-cached row sees the post-update values.
    await session.refresh(existing)
    return TriageFinding.model_validate(existing)
