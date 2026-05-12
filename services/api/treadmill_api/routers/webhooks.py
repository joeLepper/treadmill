"""Webhooks router.

POST /api/v1/webhooks/github receives GitHub webhook deliveries. The flow:

  1. Read raw body (signature verification needs the bytes pre-parse).
  2. Verify HMAC-SHA256 against ``X-Hub-Signature-256`` per ADR-0007.
  3. Parse JSON body.
  4. Normalize the (X-GitHub-Event, action) pair to internal verb +
     payload via ``treadmill_api.webhooks.normalize``.
  5. Validate the normalized payload through the Pydantic event registry
     so any drift between the normalizer and the registry surfaces here
     as a 500 (server bug, not a client problem).
  6. Look up ``task_id`` via the task_prs bridge.
  7. Insert an Event row.
  8. Publish on the event bus (log-stub at v0; SNS later).

Cache-then-heal pending-event buffering (per bunkhouse / ADR-0007) is a
follow-up; v0 simply persists with ``task_id = NULL`` if no bridge row
exists yet. The (entity_type, action) index makes catch-up scans cheap.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.config import Settings, get_settings
from treadmill_api.dependencies_db import get_session
from treadmill_api.eventbus import get_publisher
from treadmill_api.events import encode_payload, parse_payload
from treadmill_api.models import Event, TaskPR
from treadmill_api.webhooks import (
    InvalidSignatureError,
    SignatureMissingError,
    buffer_pending_event,
    normalize_github_event,
    verify_github_signature,
)

logger = logging.getLogger("treadmill.webhooks")
router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


def require_fully_local_mode(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Gate dependency: 503 unless the deployment is in ``fully_local`` mode.

    Per ADR-0017, the canonical webhook receiver in ``dev_local`` and
    ``fully_remote`` modes is the AWS-side path (API Gateway -> Lambda -> SQS
    -> the webhook-inbox poller). The in-process HTTP route only exists for
    ``fully_local`` mode, where the laptop is the only receiver and no AWS
    infrastructure is provisioned. In the other two modes this endpoint is
    intentionally disabled: 503 (rather than 404) signals "this path exists
    but is off in this mode" so a misconfigured caller gets an actionable
    error pointing at the architectural decision, not a deceptive "not
    found" suggesting the route was removed.

    The gate runs before signature verification or any body work so a
    caller who hits the wrong mode gets an immediate 503 rather than a 400
    from a missing/invalid signature.
    """
    if not settings.is_fully_local:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "webhook ingestion is via the AWS-side path in this mode; "
                "see ADR-0017"
            ),
        )


def _extract_commit_sha(action: str, body: dict[str, Any]) -> str | None:
    """Pull the HEAD-at-event-time commit SHA from a raw GitHub payload.

    Per ADR-0014, every github event whose semantics are "I happened at a
    specific HEAD" populates ``events.commit_sha`` so ADR-0013's
    ``task_mergeability`` VIEW can join on it without JSONB extraction.
    Returns ``None`` for actions that have no commit anchor.
    """
    pr = body.get("pull_request") or {}
    head = pr.get("head") or {}
    if action == "pr_opened":
        return head.get("sha") or None
    if action == "pr_synchronize":
        return head.get("sha") or None
    if action == "pr_review_submitted":
        review = body.get("review") or {}
        return review.get("commit_id") or None
    if action == "pr_merged":
        # Prefer the merge commit SHA; fall back to the PR's head SHA if
        # GitHub hasn't populated merge_commit_sha (rare but defensive).
        return pr.get("merge_commit_sha") or head.get("sha") or None
    if action == "check_run_completed":
        check_run = body.get("check_run") or {}
        return check_run.get("head_sha") or None
    return None


@router.post(
    "/github",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_fully_local_mode)],
)
async def github_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_github_event: Annotated[str | None, Header()] = None,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> dict:
    """GitHub webhook receiver.

    Operational in ``fully_local`` mode only. In ``dev_local`` and
    ``fully_remote`` modes, the ``require_fully_local_mode`` dependency
    short-circuits with a 503 before this handler body runs (see ADR-0017
    for the AWS-side canonical path).
    """
    body_bytes = await request.body()

    # 1. Signature verification.
    try:
        verify_github_signature(
            settings.github_webhook_secret, body_bytes, x_hub_signature_256
        )
    except SignatureMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    except InvalidSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    # 2. Header presence.
    if not x_github_event:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-GitHub-Event header is required",
        )

    # 3. Parse body.
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid JSON body: {exc}",
        ) from exc

    # 4. Normalize.
    normalized = normalize_github_event(x_github_event, body)
    if normalized is None:
        logger.info(
            "skipping unhandled github event: event=%s action=%s delivery=%s",
            x_github_event,
            body.get("action"),
            x_github_delivery,
        )
        return {
            "status": "skipped",
            "reason": f"unhandled event {x_github_event!r} action {body.get('action')!r}",
        }

    # 5. Validate via the Pydantic event registry. A failure here means the
    # normalizer drifted from the registry — it's a server bug, not a
    # client problem. Surface as 500 so it's visible in monitoring.
    try:
        typed_payload = parse_payload(
            normalized.entity_type, normalized.action, normalized.payload
        )
    except PydanticValidationError as exc:
        logger.exception(
            "normalizer produced payload that failed event-registry validation; "
            "event=%s action=%s",
            x_github_event,
            normalized.action,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="server normalization error",
        ) from exc

    # 6. Look up task_id via task_prs bridge.
    task_id = None
    if normalized.repo and normalized.pr_number is not None:
        result = await session.execute(
            select(TaskPR.task_id).where(
                func.lower(TaskPR.repo) == normalized.repo.lower(),
                TaskPR.pr_number == normalized.pr_number,
            )
        )
        task_id = result.scalar_one_or_none()

    # 7. Persist Event row. Per ADR-0014, populate ``commit_sha`` on every
    # github event that has a commit anchor so the mergeability VIEW
    # (ADR-0013) can join on it without JSONB extraction.
    commit_sha = _extract_commit_sha(normalized.action, body)
    event = Event(
        entity_type=normalized.entity_type,
        action=normalized.action,
        task_id=task_id,
        payload=encode_payload(typed_payload),
        commit_sha=commit_sha,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)

    # 7b. Cache-then-heal: if no task_id resolved, buffer the event in Redis
    # for replay when the task_prs bridge row eventually appears. The
    # drain function on the future task_prs INSERT path picks these up
    # and re-publishes with task_id resolved.
    if (
        task_id is None
        and normalized.repo
        and normalized.pr_number is not None
        and request.app.state.redis is not None
    ):
        try:
            await buffer_pending_event(
                request.app.state.redis,
                normalized.repo,
                normalized.pr_number,
                event.id,
            )
        except Exception:
            logger.exception(
                "pending-event buffering failed for event_id=%s repo=%s pr=%d",
                event.id, normalized.repo, normalized.pr_number,
            )

    # 8. Publish (best-effort; log-fallback when topic ARN unset).
    publisher = get_publisher()
    try:
        await publisher.publish(event, typed_payload)
    except Exception:
        logger.exception(
            "event publish failed for event_id=%s; "
            "row is persisted, consumer rescan will pick it up",
            event.id,
        )

    return {
        "status": "accepted",
        "event_id": str(event.id),
        "entity_type": event.entity_type,
        "action": event.action,
        "task_id": str(task_id) if task_id is not None else None,
        "delivery": x_github_delivery,
    }
