"""``POST /api/v1/triage/findings`` — ADR-0061 finding-ingest endpoint.

The HTTP seam between role-ui-triage's JSON output and the
``triage_findings`` table. Closes the gap ADR-0061's Step 1-6 left
open: Step 1 shipped ``TriageStore.insert_finding`` + the schema,
Step 6 shipped the labeling GET/POST, but no path existed for the
role's ``run.json`` output to actually land in the corpus.

Wire shape: a single POST accepts a list of fully-formed
``TriageFinding`` records (the role writes the run.json then POSTs
that array; per the v1.1 prompt). Each finding is validated through
the existing Pydantic model (all closed enums + the
``model_validator`` for ``suppression_signal`` and
``dispatched_plan_id``), then inserted via the shared
``TriageStore.insert_finding`` method that Step 1 already shipped.

We deliberately accept an array, not a single finding, because a
typical triage run produces 1-3 findings and POSTing them in one
transaction keeps the run's findings atomic from the labeler's
perspective (either the whole run is queryable or none of it is).
The cap at 100 findings/request is defensive — the v1 prompt's
dispatch policy bounds runs at 3 dispatched + a handful of
research_only / suppressed, so 100 is well above any reasonable run.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.schemas.triage_finding import TriageFinding
from treadmill_api.triage_store import TriageStore


router = APIRouter()


class CreateFindingsRequest(BaseModel):
    """Body shape for ``POST /api/v1/triage/findings``.

    A list of fully-formed TriageFinding records. The role writes them
    to its run.json artifact and POSTs the array; each record is
    validated through the schema's closed enums + model validators
    before any DB write so a partial-batch insert can't leave the
    corpus in an inconsistent state.
    """

    findings: list[TriageFinding] = Field(..., min_length=1, max_length=100)


class CreateFindingsResponse(BaseModel):
    """Returned on a successful POST.

    Carries the inserted finding ids in input order so the caller can
    join them back to the source run.json by index. ``count`` is a
    convenience for shell-script callers that just want a quick "did
    they all land?" check.
    """

    finding_ids: list[uuid.UUID]
    count: int


@router.post(
    "/findings",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateFindingsResponse,
)
async def create_findings(
    body: CreateFindingsRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CreateFindingsResponse:
    """Persist a batch of TriageFinding records.

    All findings in the batch are inserted in one transaction; if
    any individual insert fails (e.g. a UUID collision on
    ``finding_id``) the whole batch rolls back and the caller gets
    a 409 with the offending id. This is the simplest semantics
    that preserves "either the whole run is queryable or none of
    it is" — the caller's failure recovery is "fix the collision,
    re-POST the array."
    """
    store = TriageStore()
    inserted: list[uuid.UUID] = []
    try:
        for finding in body.findings:
            finding_id = await store.insert_finding(session, finding)
            inserted.append(finding_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # UUID collision on PK is the only constraint a well-formed
        # batch can plausibly trip (the schema's CHECK constraints are
        # already enforced by Pydantic). Surface the offending id when
        # it's parseable from the driver's message; fall back to a
        # generic 409 otherwise.
        msg = str(exc.orig) if exc.orig else str(exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"triage_findings INSERT failed: {msg}",
        ) from exc

    return CreateFindingsResponse(finding_ids=inserted, count=len(inserted))
