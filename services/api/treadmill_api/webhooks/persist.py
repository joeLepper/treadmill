"""Shared persist + resolve flow for normalized webhook events (ADR-0063 Step 3).

Treadmill ingests GitHub webhooks through two paths — the in-process HTTP
route at ``routers/webhooks.py`` and the SQS-based inbox poller at
``coordination/webhook_inbox.py`` (added by ADR-0049). Pre-ADR-0063 both
paths inlined the same three-step sequence on every normalized event:

  1. task_prs SELECT (resolve ``task_id`` from ``(repo, pr_number)``).
  2. Event INSERT (with the resolved ``task_id`` or ``NULL``).
  3. On miss with Redis available, push the new ``event_id`` onto the
     pending list so the consumer's future ``task_prs`` INSERT drains it.

The two paths drifted twice — the 2026-05-14 dual-ingress drift
learning and the 2026-05-29 Task 3b 40-minute stall (the SQS path was
persisting Events with ``task_id=NULL`` and never buffering). ADR-0063
makes the sequence structurally single-sourced: both ingress paths
call ``persist_and_resolve_webhook_event`` and the lock-step guarantee
holds by construction.

The helper also owns publishing on the event bus and the publish
error contract (log + swallow; the persisted row is the source of
truth, the consumer's rescan recovers fan-out).

Idempotency note: the SQS path derives a deterministic ``event_id``
from ``X-GitHub-Delivery`` (ADR-0017 §"Deterministic event_id derivation")
and wants ``ON CONFLICT (id) DO NOTHING`` semantics so SQS visibility-
timeout redeliveries collapse onto the same row. The HTTP path has no
such anchor and uses the database-generated UUID. The helper accepts an
optional ``event_id`` kwarg; when set it uses ``pg_insert(...).on_conflict_
do_nothing(index_elements=["id"])``, otherwise it does a plain
``session.add(Event(...))``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.eventbus import EventPublisher
from treadmill_api.events import encode_payload, parse_payload
from treadmill_api.ci_observer import maybe_emit_ci_result
from treadmill_api.models import Event, TaskPR
from treadmill_api.webhooks.normalize import NormalizationResult
from treadmill_api.webhooks.pending_events import (
    buffer_pending_event,
    pr_pending_buffer_key,
)

logger = logging.getLogger("treadmill.webhooks.persist")


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


async def persist_and_resolve_webhook_event(
    session: AsyncSession,
    normalized: NormalizationResult,
    body_json: dict[str, Any],
    redis_client: Any,
    publisher: EventPublisher,
    *,
    event_id: uuid.UUID | None = None,
) -> Event:
    """Lookup task_prs → persist Event → buffer-on-miss → publish.

    The single shared seam every webhook ingress path uses for ADR-0063
    Step 3. Both ``routers/webhooks.py`` and ``coordination/webhook_inbox.py``
    must go through here so the FK-resolution + buffer-on-miss invariant
    cannot drift between the two ingress paths again (see ADR-0063 and
    the 2026-05-29 Task 3b incident).

    Args:
        session: Async SQLAlchemy session. The caller owns lifecycle; the
            helper commits before returning so the returned ``Event`` is
            already durable.
        normalized: The output of ``normalize_github_event``. Carries the
            entity_type / action / payload dict that the registry will
            validate, plus the convenience ``repo`` + ``pr_number`` for
            task_prs resolution.
        body_json: The raw parsed JSON body — needed for the per-action
            commit_sha extraction (ADR-0014). Distinct from
            ``normalized.payload`` because some commit anchors (e.g.
            ``check_run.head_sha``) live outside the normalized shape.
        redis_client: Optional async Redis client. ``None`` skips the
            buffer-on-miss call cleanly (narrow tests / log-only deployments
            without Redis wiring).
        publisher: Event-bus publisher. The helper calls ``publish(event,
            typed)`` once; publish failure logs and returns successfully —
            the Event row is the source of truth and the consumer's rescan
            picks it up (mirrors ADR-0011 semantics).
        event_id: Optional deterministic Event PK. When provided, the
            INSERT is an upsert with ``ON CONFLICT (id) DO NOTHING`` so
            re-deliveries collapse onto the same row (the SQS path uses
            this for ADR-0017's deterministic event_id derivation). When
            ``None``, a fresh database-generated UUID is used (the HTTP
            path — no replay anchor).

    Returns:
        The persisted ``Event`` so callers can include ``event_id`` in
        their HTTP responses or log lines.

    Raises:
        pydantic.ValidationError: When ``normalized.payload`` fails the
            event-registry's typed validation. Signals a normalizer ↔
            registry drift (a server bug); callers map this to whichever
            status fits their ingress contract (500 for the HTTP route;
            propagation for the SQS path so the message bounces back to
            the queue for an operator-driven fix).
    """
    typed = parse_payload(
        normalized.entity_type, normalized.action, normalized.payload,
    )
    commit_sha = _extract_commit_sha(normalized.action, body_json)

    task_id: uuid.UUID | None = None
    if normalized.repo and normalized.pr_number is not None:
        result = await session.execute(
            select(TaskPR.task_id).where(
                func.lower(TaskPR.repo) == normalized.repo.lower(),
                TaskPR.pr_number == normalized.pr_number,
            )
        )
        task_id = result.scalar_one_or_none()

    encoded = encode_payload(typed)

    if event_id is None:
        event = Event(
            entity_type=normalized.entity_type,
            action=normalized.action,
            task_id=task_id,
            payload=encoded,
            commit_sha=commit_sha,
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
    else:
        stmt = (
            pg_insert(Event)
            .values(
                id=event_id,
                entity_type=normalized.entity_type,
                action=normalized.action,
                task_id=task_id,
                payload=encoded,
                commit_sha=commit_sha,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.execute(stmt)
        await session.commit()
        # Re-fetch so the publisher sees the canonical row (the INSERT
        # may have been a no-op on re-delivery; we want the existing
        # row's fields, not the proposed ones).
        existing = await session.get(Event, event_id)
        if existing is None:
            # Should never happen — the row was either inserted by us or
            # already existed. Raising lets the SQS path bounce the message
            # for retry; the HTTP path treats it as a 500-class server bug.
            raise RuntimeError(
                f"Event row {event_id} vanished after upsert",
            )
        event = existing

    # Buffer on miss (cache-then-heal per ADR-0007 / ADR-0063). The Event
    # is already persisted; a Redis hiccup degrades the race window but
    # never loses audit data. Guarded by ``redis_client is not None`` so
    # deployments and narrow tests without Redis wiring don't crash the
    # ingest path.
    if (
        task_id is None
        and normalized.repo
        and normalized.pr_number is not None
        and redis_client is not None
    ):
        try:
            await buffer_pending_event(
                redis_client,
                pr_pending_buffer_key(normalized.repo, normalized.pr_number),
                event.id,
            )
            logger.info(
                "buffered pending event_id=%s for repo=%s pr=%d "
                "(task_prs miss; awaiting bridge row to drain)",
                event.id, normalized.repo, normalized.pr_number,
            )
        except Exception:
            logger.exception(
                "pending-event buffering failed for event_id=%s repo=%s pr=%d; "
                "row is persisted",
                event.id, normalized.repo, normalized.pr_number,
            )

    # Publish (best-effort). The Event row is the source of truth; on
    # failure the consumer's rescan by (entity_type, action) index picks
    # it up. Same contract both ingress paths have always had.
    try:
        await publisher.publish(event, typed)
    except Exception:
        logger.exception(
            "event publish failed for event_id=%s; "
            "row is persisted, consumer rescan will pick it up",
            event.id,
        )

    # head_sha writer (task 5dd4a32d, decided from the #335 gap): keep
    # ``task_prs.head_sha`` current at ingest so the resolver's fast path
    # works for first-push CI. pr_opened can land BEFORE the coordinator
    # registers the task_prs row — that race is covered by the
    # ci-observer's events-join fallback, so a missed UPDATE here is
    # self-healing, not a loss.
    if (
        normalized.action in ("pr_opened", "pr_synchronize")
        and normalized.repo
        and normalized.pr_number is not None
    ):
        head_sha = normalized.payload.get("head_sha")
        if head_sha:
            try:
                from sqlalchemy import update

                await session.execute(
                    update(TaskPR)
                    .where(
                        func.lower(TaskPR.repo) == normalized.repo.lower(),
                        TaskPR.pr_number == normalized.pr_number,
                    )
                    .values(head_sha=head_sha)
                )
                await session.commit()
            except Exception:
                logger.exception(
                    "task_prs.head_sha update failed for %s#%s; "
                    "observer fallback covers attribution",
                    normalized.repo, normalized.pr_number,
                )

    # CI-observer (ADR-0090, task 5dd4a32d): one task.ci_result per
    # completed check SUITE. Internally guarded — never raises into the
    # ingress; an unattributable or still-running suite is a no-op.
    if normalized.action == "check_run_completed":
        await maybe_emit_ci_result(session, publisher, normalized.payload)

    return event
