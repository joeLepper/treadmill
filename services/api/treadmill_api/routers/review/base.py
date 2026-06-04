"""``build_review_router`` — factory for per-kind operator review endpoints.

Per ADR-0070, every "operator sanity-checks an LLM proposal" surface shares
the same four-endpoint shape. This factory injects the per-kind parameters
(table class, input/output models, verdict column name) and returns a
configured ``APIRouter`` that the per-kind module assigns to ``router``.

This module intentionally does NOT assign a module-level ``router`` so the
``routers/review/__init__.py`` auto-discovery loop ignores it — the loop
only mounts modules where ``getattr(module, "router", None) is APIRouter``.

Note: ``from __future__ import annotations`` is intentionally absent here.
FastAPI resolves body-parameter type annotations via ``typing.get_type_hints``
which evaluates annotation strings in module-level globals only — closure
variables (like ``label_input_model``) are invisible there. Without the
future import, Python evaluates ``body: label_input_model`` at function-
definition time while the variable is still in scope, so FastAPI sees the
actual class rather than an unresolvable string.
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.services import review_stats


def build_review_router(
    *,
    prefix: str,
    row_cls: type,
    label_input_model: type[BaseModel],
    output_model: type[BaseModel],
    verdict_attr: str,
    llm_label_attr: str = "llm_label",
) -> APIRouter:
    """Return an ``APIRouter`` with the four ADR-0070 review endpoints.

    Parameters
    ----------
    prefix:
        Full path prefix for this kind, e.g. ``/architect-gold``. The
        factory mounts it as ``APIRouter(prefix=prefix)``.
    row_cls:
        SQLAlchemy declarative class (subclass of ReviewQueueRowMixin + Base).
    label_input_model:
        Per-kind Pydantic ``BaseModel``; ``model_dump()`` is splatted onto
        the row on ``POST /{id}/label``.  Must include ``labeled_by``.
    output_model:
        Per-kind Pydantic ``BaseModel`` used as the endpoint response type.
        The per-kind module owns the wire schema.
    verdict_attr:
        Name of the column the operator's verdict is written to.
    llm_label_attr:
        Name of the column carrying the LLM recommendation (default
        ``"llm_label"``), used for accuracy math in ``GET /stats``.

    Route registration order
    ------------------------
    Literal-path routes (``/next``, ``/stats``) are registered BEFORE the
    parameterized routes (``/{id}``, ``/{id}/label``) so FastAPI matches
    ``/stats`` to the stats handler rather than the ``/{id}`` handler with
    ``id="stats"`` (which would 422 on UUID parse).
    """
    router: APIRouter = APIRouter(prefix=prefix)

    verdict_col = getattr(row_cls, verdict_attr)
    confidence_col = getattr(row_cls, "llm_confidence")
    created_at_col = getattr(row_cls, "created_at")

    # Confidence ordering: low < medium < high (deterministic CASE expression).
    _confidence_order = case(
        (confidence_col == "low", 1),
        (confidence_col == "medium", 2),
        (confidence_col == "high", 3),
        else_=4,
    )

    # ── GET /next ─────────────────────────────────────────────────────────────

    @router.get("/next", response_model=list[output_model])  # type: ignore[valid-type]
    async def get_next(
        session: Annotated[AsyncSession, Depends(get_session)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[Any]:
        """Return up to ``limit`` unlabeled rows ordered by confidence ASC
        then ``created_at`` ASC (low confidence surfaces first so the
        operator reviews the LLM's least-certain proposals early).
        """
        stmt = (
            select(row_cls)
            .where(verdict_col.is_(None))
            .order_by(_confidence_order, created_at_col)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    # ── GET /stats ────────────────────────────────────────────────────────────

    @router.get("/stats", response_model=review_stats.StatsResponse)
    async def get_stats(
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> review_stats.StatsResponse:
        """Return aggregated labeling statistics for this review kind."""
        return await review_stats.compute_stats(
            session,
            row_cls=row_cls,
            verdict_attr=verdict_attr,
            llm_label_attr=llm_label_attr,
        )

    # ── GET /{id} ─────────────────────────────────────────────────────────────

    @router.get("/{row_id}", response_model=output_model)
    async def get_one(
        row_id: uuid.UUID,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Any:
        """Return one row by primary key; 404 when missing."""
        result = await session.execute(
            select(row_cls).where(row_cls.id == row_id)
        )
        row = result.scalars().one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{row_cls.__tablename__} {row_id} not found",
            )
        return row

    # ── POST /{id}/label ──────────────────────────────────────────────────────

    @router.post("/{row_id}/label", response_model=output_model)
    async def label_row(
        row_id: uuid.UUID,
        body: label_input_model,  # type: ignore[valid-type]
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Any:
        """Persist operator verdict + metadata for ``row_id``.

        ``labeled_by`` in the body is required.  All other label fields are
        optional (null = skip signal per ADR-0070).  Returns the refreshed
        row so the UI always sees server-stamped ``labeled_at``.
        """
        result = await session.execute(
            select(row_cls).where(row_cls.id == row_id)
        )
        row = result.scalars().one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{row_cls.__tablename__} {row_id} not found",
            )

        # Splat all fields from the input model onto the row.
        for field_name, value in body.model_dump().items():
            setattr(row, field_name, value)

        # Server-stamp labeled_at.
        row.labeled_at = datetime.now(timezone.utc)

        await session.commit()
        await session.refresh(row)
        return row

    return router
