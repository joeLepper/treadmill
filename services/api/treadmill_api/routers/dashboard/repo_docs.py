"""``GET /api/v1/dashboard/repos/{repo:path}/docs`` — Per-repo doc summary.

Mirrors ``services/dashboard/src/api/queries.ts`` ``useRepoDocs`` 1:1:
``{ arch, plans, last_updated }`` per ``src/api/types.ts`` ``RepoDocs``.

Data source: the ADR-0054 ``repo_context_docs`` index — read via
``OnboardingStore.list_repo_docs``. Doc bodies in S3 stay off the API
hot path; this surface only consults the index, so it scales the same
way the LIST endpoint in ``routers/context_docs.py`` does.

Failure modes are explicit (no silent empty payloads):

  * ``CONTEXT_DOCS_BUCKET`` unset → **503** (same pattern as
    ``routers/context_docs.py``; the dependency raises before the handler
    runs).
  * Repo has no indexed docs → **404** (ADR-0054 "absent data → 404,
    not empty payload"). Without this the dashboard would happily render
    a "0 plans, never updated" card for a repo the system has never
    onboarded — a false-positive presence claim.

``arch`` is the doc_path string for the repo's ``arch.md`` (matches the
mock's shape — see ``services/dashboard/src/api/mock.ts`` ``REPO_DOCS``).
Empty string when an arch doc isn't present but other docs are; the
dashboard's ``DocLink`` row treats the empty as "no arch link to
follow". Paths mirror the in-repo layout (ADR-0054 d.3) — ``arch.md`` at
root or under a context dir (e.g. ``.treadmill/arch.md``) both count.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.onboarding_store import OnboardingStore
from treadmill_api.routers.context_docs import get_context_store


router = APIRouter()


class RepoDocsResponse(BaseModel):
    """Per-repo doc summary surfaced on the operator dashboard.

    Field-for-field with ``services/dashboard/src/api/types.ts``
    ``RepoDocs``; pydantic enforces the contract at the wire so a drift
    in either side is a test failure rather than a silent UI bug.
    """

    arch: str
    plans: int
    last_updated: datetime


def _arch_doc_path(doc_paths: list[str]) -> str:
    """Return the path of the repo's arch.md doc, or ``''`` if absent.

    Match any doc_path whose basename is ``arch.md``. When more than one
    matches (rare — a context dir + a root copy), prefer the shortest so
    the dashboard's "where's the arch doc" answer is stable across
    re-orderings of the index.
    """
    candidates = sorted(
        (p for p in doc_paths if p == "arch.md" or p.endswith("/arch.md")),
        key=len,
    )
    return candidates[0] if candidates else ""


@router.get(
    "/repos/{repo:path}/docs",
    response_model=RepoDocsResponse,
    dependencies=[Depends(get_context_store)],
)
async def get_repo_docs(
    repo: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RepoDocsResponse:
    rows = await OnboardingStore().list_repo_docs(session, repo)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no context docs indexed for repo {repo!r}",
        )
    doc_paths = [row.doc_path for row in rows]
    return RepoDocsResponse(
        arch=_arch_doc_path(doc_paths),
        plans=sum(1 for p in doc_paths if p.startswith("plans/")),
        last_updated=max(row.created_at for row in rows),
    )
