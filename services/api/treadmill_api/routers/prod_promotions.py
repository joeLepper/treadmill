"""Prod-promotion gate router — ADR-0088.

The human gate for production deploys, implementing the contract at
``docs/plans/2026-06-10-prod-promotion-gate-contract.md``:

POST   /api/v1/prod_promotions                 — coordinator proposes (bundle)
GET    /api/v1/prod_promotions                 — list (newest first, filterable)
GET    /api/v1/prod_promotions/{proposal_id}   — current status + bundle
                                                 (the workflow re-verify read)
POST   /api/v1/prod_promotions/{proposal_id}/approve  — OPERATOR-KEYED
POST   /api/v1/prod_promotions/{proposal_id}/reject   — OPERATOR-KEYED
POST   /api/v1/prod_promotions/{proposal_id}/transition — workflow reports
       started/succeeded/failed (CAS-guarded, unkeyed like propose)

The gate lives in the state machine, not in prose: approve/reject require
``X-Operator-Key`` matching ``TREADMILL_OPERATOR_KEY`` from the API env —
the key exists only on the operator's machine, so coordinator/worker
sessions structurally cannot approve. Status transitions are
compare-and-swap (``WHERE status = :expected AND expires_at > now()``);
single-use and the expired-but-undecided hole both die in the guard.
Reads apply lazy expiry: an undecided row past ``expires_at`` flips to
``expired`` (+ emits the event) on first read.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.config import get_settings
from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher
from treadmill_api.events.prod_promotion import (
    ProdPromotionApproved,
    ProdPromotionExpired,
    ProdPromotionFailed,
    ProdPromotionProposed,
    ProdPromotionRejected,
    ProdPromotionStarted,
    ProdPromotionSucceeded,
)
from treadmill_api.models import ProdPromotion

router = APIRouter(prefix="/api/v1/prod_promotions", tags=["prod_promotions"])


# ── Schemas ──────────────────────────────────────────────────────────────────


class ProposeBody(BaseModel):
    """The propose bundle, validated by the ProdPromotionProposed payload
    shape minus the server-assigned proposal_id."""

    model_config = ConfigDict(extra="forbid")

    repo: str
    env_from: str
    env_to: str
    digests: list[dict[str, Any]]
    staging_evidence: dict[str, Any]
    diff_summary: list[str]
    diff_anchor: str
    expires_at: datetime
    proposed_by: str


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decided_by: str
    note: str | None = None
    reason: str | None = None


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str  # started | succeeded | failed
    workflow_run_id: str | None = None
    sha: str | None = None
    digests: list[dict[str, Any]] | None = None
    reason: str | None = None


def _row_out(row: ProdPromotion) -> dict[str, Any]:
    return {
        "proposal_id": str(row.proposal_id),
        "repo": row.repo,
        "status": row.status,
        "bundle": row.bundle,
        "expires_at": row.expires_at.isoformat(),
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "decision_note": row.decision_note,
        "created_at": row.created_at.isoformat(),
    }


# ── Operator-key gate ────────────────────────────────────────────────────────


def _require_operator_key(x_operator_key: str | None) -> None:
    """403 unless the header matches TREADMILL_OPERATOR_KEY.

    503 when the deployment has no key configured — an unconfigured gate
    fails CLOSED, never open.
    """
    configured = get_settings().operator_key
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="prod-promotion gate has no operator key configured; "
            "set TREADMILL_OPERATOR_KEY on the API environment",
        )
    if not x_operator_key or not hmac.compare_digest(x_operator_key, configured):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid or missing X-Operator-Key",
        )


# ── Lazy expiry ──────────────────────────────────────────────────────────────


async def _lazy_expire(
    session: AsyncSession, dispatcher: Dispatcher, row: ProdPromotion
) -> ProdPromotion:
    """Flip an undecided row past expires_at to expired (CAS) + emit."""
    if row.status != "proposed":
        return row
    if row.expires_at > datetime.now(timezone.utc):
        return row
    result = await session.execute(
        update(ProdPromotion)
        .where(
            ProdPromotion.proposal_id == row.proposal_id,
            ProdPromotion.status == "proposed",
        )
        .values(status="expired")
        .returning(ProdPromotion.proposal_id)
    )
    if result.first() is not None:
        await dispatcher.persist_and_publish(
            session,
            entity_type="prod_promotion",
            action="expired",
            payload=ProdPromotionExpired(
                proposal_id=row.proposal_id, repo=row.repo
            ),
        )
        await session.commit()
    await session.refresh(row)
    return row


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("", status_code=status.HTTP_201_CREATED)
async def propose(
    body: ProposeBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> dict[str, Any]:
    row = ProdPromotion(
        repo=body.repo,
        bundle=body.model_dump(mode="json"),
        expires_at=body.expires_at,
    )
    session.add(row)
    await session.flush()
    payload = ProdPromotionProposed(
        proposal_id=row.proposal_id, **body.model_dump()
    )
    await dispatcher.persist_and_publish(
        session,
        entity_type="prod_promotion",
        action="proposed",
        payload=payload,
    )
    await session.commit()
    await session.refresh(row)
    return _row_out(row)


@router.get("")
async def list_proposals(
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    repo: str | None = None,
    status_filter: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    stmt = select(ProdPromotion).order_by(ProdPromotion.created_at.desc())
    if repo:
        stmt = stmt.where(ProdPromotion.repo == repo)
    stmt = stmt.limit(min(limit, 100))
    rows = (await session.execute(stmt)).scalars().all()
    out = []
    for row in rows:
        row = await _lazy_expire(session, dispatcher, row)
        if status_filter and row.status != status_filter:
            continue
        out.append(_row_out(row))
    return out


@router.get("/{proposal_id}")
async def get_proposal(
    proposal_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> dict[str, Any]:
    row = await session.get(ProdPromotion, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    row = await _lazy_expire(session, dispatcher, row)
    return _row_out(row)


@router.post("/{proposal_id}/approve")
async def approve(
    proposal_id: uuid.UUID,
    body: DecisionBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    x_operator_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_operator_key(x_operator_key)
    row = await session.get(ProdPromotion, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")

    # Idempotent re-approve: the CLI may re-run after a dispatch failure
    # (ADR-0088 §3) — return current state without a second transition.
    if row.status == "approved":
        return _row_out(row)

    result = await session.execute(
        update(ProdPromotion)
        .where(
            ProdPromotion.proposal_id == proposal_id,
            ProdPromotion.status == "proposed",
            ProdPromotion.expires_at > datetime.now(timezone.utc),
        )
        .values(
            status="approved",
            decided_by=body.decided_by,
            decided_at=datetime.now(timezone.utc),
            decision_note=body.note,
        )
        .returning(ProdPromotion.proposal_id)
    )
    if result.first() is None:
        row = await _lazy_expire(session, dispatcher, row)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"proposal is {row.status}, not approvable",
        )
    await dispatcher.persist_and_publish(
        session,
        entity_type="prod_promotion",
        action="approved",
        payload=ProdPromotionApproved(
            proposal_id=proposal_id,
            repo=row.repo,
            decided_by=body.decided_by,
            note=body.note,
        ),
    )
    await session.commit()
    await session.refresh(row)
    return _row_out(row)


@router.post("/{proposal_id}/reject")
async def reject(
    proposal_id: uuid.UUID,
    body: DecisionBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    x_operator_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_operator_key(x_operator_key)
    if not body.reason:
        raise HTTPException(status_code=422, detail="reject requires a reason")
    row = await session.get(ProdPromotion, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    result = await session.execute(
        update(ProdPromotion)
        .where(
            ProdPromotion.proposal_id == proposal_id,
            ProdPromotion.status == "proposed",
            ProdPromotion.expires_at > datetime.now(timezone.utc),
        )
        .values(
            status="rejected",
            decided_by=body.decided_by,
            decided_at=datetime.now(timezone.utc),
            decision_note=body.reason,
        )
        .returning(ProdPromotion.proposal_id)
    )
    if result.first() is None:
        row = await _lazy_expire(session, dispatcher, row)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"proposal is {row.status}, not rejectable",
        )
    await dispatcher.persist_and_publish(
        session,
        entity_type="prod_promotion",
        action="rejected",
        payload=ProdPromotionRejected(
            proposal_id=proposal_id,
            repo=row.repo,
            decided_by=body.decided_by,
            reason=body.reason,
        ),
    )
    await session.commit()
    await session.refresh(row)
    return _row_out(row)


# Legal CAS transitions for the workflow-reported tail of the lifecycle.
_TRANSITIONS: dict[str, str] = {
    "started": "approved",
    "succeeded": "started",
    "failed": "started",
}


@router.post("/{proposal_id}/transition")
async def transition(
    proposal_id: uuid.UUID,
    body: TransitionBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> dict[str, Any]:
    """promote-to-prod.yml reports started/succeeded/failed.

    Unkeyed (same trust level as propose): the workflow only ever runs
    against an already-approved proposal, and the CAS guard makes a
    transition from any other state a 409 — reporting state is not a
    privilege escalation. The expiry predicate is deliberately absent
    here: an approved run may legitimately finish after expires_at.
    """
    expected = _TRANSITIONS.get(body.action)
    if expected is None:
        raise HTTPException(status_code=422, detail=f"unknown action {body.action!r}")
    row = await session.get(ProdPromotion, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    result = await session.execute(
        update(ProdPromotion)
        .where(
            ProdPromotion.proposal_id == proposal_id,
            ProdPromotion.status == expected,
        )
        .values(status=body.action)
        .returning(ProdPromotion.proposal_id)
    )
    if result.first() is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"proposal is {row.status}; {body.action} requires {expected}",
        )
    payload: Any
    if body.action == "started":
        payload = ProdPromotionStarted(
            proposal_id=proposal_id,
            repo=row.repo,
            workflow_run_id=body.workflow_run_id,
        )
    elif body.action == "succeeded":
        if not body.sha or body.digests is None:
            raise HTTPException(
                status_code=422, detail="succeeded requires sha + digests"
            )
        payload = ProdPromotionSucceeded(
            proposal_id=proposal_id,
            repo=row.repo,
            sha=body.sha,
            digests=body.digests,
        )
    else:
        if not body.reason:
            raise HTTPException(status_code=422, detail="failed requires a reason")
        payload = ProdPromotionFailed(
            proposal_id=proposal_id, repo=row.repo, reason=body.reason
        )
    await dispatcher.persist_and_publish(
        session,
        entity_type="prod_promotion",
        action=body.action,
        payload=payload,
    )
    await session.commit()
    await session.refresh(row)
    return _row_out(row)
