"""Context-doc REST API — ADR-0054.

Per-repo context docs are content-addressed in S3 (``ContextStore``, ADR-0050
d.4) and indexed in Postgres (``repo_context_docs`` via ``OnboardingStore``).
This router is the thin HTTP surface that the worker / CLI use to read and
write those docs.

PUT writes content to S3 and inserts the next version row. GET returns a
presigned URL that the client follows to fetch the blob — keeping doc
bodies off the API hot path. LIST returns the latest version per doc_path.

When ``CONTEXT_DOCS_BUCKET`` is unset the endpoints return 503, mirroring
the GitHub App "service not configured" pattern in ``routers/github.py``.
"""

from __future__ import annotations

import hashlib
from typing import Annotated

import boto3
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.context_store import ContextStore
from treadmill_api.dependencies_db import get_session
from treadmill_api.models.onboarding import RepoContextDocRow
from treadmill_api.onboarding_store import OnboardingStore


router = APIRouter(prefix="/api/v1/repos", tags=["context-docs"])


def get_context_store(request: Request) -> ContextStore:
    """Build a per-request ``ContextStore`` from the configured bucket.

    503 when ``CONTEXT_DOCS_BUCKET`` is unset — mirrors the GitHub App
    "service not configured" pattern in ``routers/github.py``.
    """
    settings = request.app.state.settings
    bucket = getattr(settings, "context_docs_bucket", None)
    if not bucket:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Context-doc store not configured (CONTEXT_DOCS_BUCKET unset)",
        )
    s3_client = boto3.client("s3", region_name=settings.aws_region)
    return ContextStore(s3_client, bucket)


class PutContextDocRequest(BaseModel):
    content: str = Field(..., min_length=1)


class PutContextDocResponse(BaseModel):
    repo: str
    doc_path: str
    version: int


class GetContextDocResponse(BaseModel):
    repo: str
    doc_path: str
    version: int
    url: str


class ContextDocSummary(BaseModel):
    doc_path: str
    version: int


class ListContextDocsResponse(BaseModel):
    repo: str
    docs: list[ContextDocSummary]


@router.put(
    "/{repo:path}/docs/{doc_path:path}",
    response_model=PutContextDocResponse,
)
async def put_context_doc(
    repo: str,
    doc_path: str,
    body: PutContextDocRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[ContextStore, Depends(get_context_store)],
) -> PutContextDocResponse:
    content_sha = hashlib.sha256(body.content.encode()).hexdigest()
    key = store.put_doc(repo, body.content)
    version = await OnboardingStore().record_context_doc(
        session, repo, doc_path, key, content_sha,
    )
    await session.commit()
    return PutContextDocResponse(repo=repo, doc_path=doc_path, version=version)


@router.get(
    "/{repo:path}/docs/{doc_path:path}",
    response_model=GetContextDocResponse,
)
async def get_context_doc(
    repo: str,
    doc_path: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[ContextStore, Depends(get_context_store)],
) -> GetContextDocResponse:
    row = await OnboardingStore().get_context_doc(session, repo, doc_path)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"context doc {doc_path!r} not found for repo {repo!r}",
        )
    return GetContextDocResponse(
        repo=repo,
        doc_path=doc_path,
        version=row.version,
        url=store.presigned_get_url(row.s3_key),
    )


@router.get(
    "/{repo:path}/docs",
    response_model=ListContextDocsResponse,
    dependencies=[Depends(get_context_store)],
)
async def list_context_docs(
    repo: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ListContextDocsResponse:
    result = await session.execute(
        select(
            RepoContextDocRow.doc_path,
            func.max(RepoContextDocRow.version).label("version"),
        )
        .where(RepoContextDocRow.repo == repo)
        .group_by(RepoContextDocRow.doc_path)
        .order_by(RepoContextDocRow.doc_path)
    )
    docs = [
        ContextDocSummary(doc_path=row.doc_path, version=row.version)
        for row in result.all()
    ]
    return ListContextDocsResponse(repo=repo, docs=docs)
