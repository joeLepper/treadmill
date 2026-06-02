"""Webhooks router.

POST /api/v1/webhooks/github receives GitHub webhook deliveries. The flow:

  1. Read raw body (signature verification needs the bytes pre-parse).
  2. Verify HMAC-SHA256 against ``X-Hub-Signature-256`` per ADR-0007.
  3. Parse JSON body.
  4. Normalize the (X-GitHub-Event, action) pair to internal verb +
     payload via ``treadmill_api.webhooks.normalize``.
  5. Hand off to ``persist_and_resolve_webhook_event`` (ADR-0063 Step 3)
     which validates the typed payload, resolves ``task_id`` via the
     task_prs bridge, persists the Event row, buffers on miss for the
     cache-then-heal flow, and publishes on the event bus.

The shared helper is the single seam both ingress paths use; the SQS
poller at ``coordination/webhook_inbox.py`` calls it too so the FK
resolution + buffer-on-miss contract cannot drift between the two.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.config import Settings, get_settings
from treadmill_api.dependencies_db import get_session
from treadmill_api.eventbus import get_publisher
from treadmill_api.webhooks import (
    InvalidSignatureError,
    SignatureMissingError,
    normalize_github_event,
    persist_and_resolve_webhook_event,
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

    # 5. Delegate to the shared persist/resolve/publish helper (ADR-0063
    # Step 3). A ValidationError from the typed registry surfaces as 500
    # — it signals a normalizer ↔ registry drift (server bug, not a
    # client problem).
    try:
        event = await persist_and_resolve_webhook_event(
            session,
            normalized,
            body,
            request.app.state.redis,
            get_publisher(),
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

    return {
        "status": "accepted",
        "event_id": str(event.id),
        "entity_type": event.entity_type,
        "action": event.action,
        "task_id": str(event.task_id) if event.task_id is not None else None,
        "delivery": x_github_delivery,
    }
