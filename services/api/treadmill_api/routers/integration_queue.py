"""``GET /api/v1/integration_queue`` — the ADR-0119 api→host-integrator contract.

The host-side integrator (running as the operator on rainbow) polls this to learn which approved
tasks to merge. The selection is single-sourced in
``coordination.integration_queue.approved_integration_candidates`` so the host never re-implements
the ADR-0118/#420 rules. Read-only: escalation on an invalid slug or a conflict is the host's job
(it POSTs the escalation event), keeping this endpoint side-effect free.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.coordination.integration_queue import approved_integration_candidates
from treadmill_api.dependencies_db import get_session

router = APIRouter(prefix="/api/v1", tags=["integration"])


class IntegrationCandidateResponse(BaseModel):
    task_id: str
    repo: str
    pr_number: int | None
    head_sha: str
    integration_base: str
    slug_valid: bool
    integration_branch: str | None


@router.get("/integration_queue", response_model=list[IntegrationCandidateResponse])
async def integration_queue(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[IntegrationCandidateResponse]:
    """Approved-but-not-integrated feature-branch tasks, latest head per task, with the derived
    integration branch (ADR-0119). Empty in steady state / when the router is dark."""
    candidates = await approved_integration_candidates(session)
    return [
        IntegrationCandidateResponse(
            task_id=c.task_id,
            repo=c.repo,
            pr_number=c.pr_number,
            head_sha=c.head_sha,
            integration_base=c.integration_base,
            slug_valid=c.slug_valid,
            integration_branch=c.integration_branch,
        )
        for c in candidates
    ]
