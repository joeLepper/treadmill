"""Webhook-inbox poller — drains the AWS-side webhook SQS queue (ADR-0017).

Sibling of ``coordination/consumer.py`` and ``coordination/replay.py``.
The Lambda webhook receiver (``infra/lambdas/webhook_receiver/handler.py``)
wraps each GitHub-delivered HTTP request into a ``WebhookInboxEnvelope``
and writes it to the webhook-inbox SQS queue. This module long-polls
that queue, validates the envelope, verifies the HMAC signature, derives
a deterministic ``event_id`` from ``X-GitHub-Delivery``, normalizes the
payload via the existing ``webhooks/normalize.py`` (the same code path
the HTTP route at ``routers/webhooks.py`` uses today), persists the
Event row, and publishes through the existing ``SNSEventPublisher``.

The poller is started by the API lifespan handler when
``settings.deployment_mode`` is ``dev_local`` or ``fully_remote`` AND
``settings.webhook_inbox_queue_url`` + ``settings.github_webhook_secret_name``
are both set. In ``fully_local`` mode there is no inbox queue (webhooks
arrive via the in-process HTTP route at ``POST /api/v1/webhooks/github``).

Phase-3-closure-fixed behaviors inherited from ``CoordinationConsumer``
(enumerated explicitly per ADR-0017 §"The local poller", so the
implementation cannot silently drift):

* **Exponential backoff** on consecutive SQS poll failures, following
  the ``1, 2, 4, 8, 16, 30`` second ladder. The counter resets on the
  first successful poll.
* **Escalated logging** after ``_FAILURES_BEFORE_ERROR_LOG`` consecutive
  failures (warn → error).
* **Granular health status** reportable via ``WebhookInboxProbe`` so
  ``/healthz`` flips to 503 when the poller is dead.
* **Poison-safe deletion** for malformed envelopes, missing
  ``x-github-delivery``, and signature failures — all three are
  permanent rejections (re-delivery would fail the same way), so we
  delete the SQS message rather than letting it bounce to the DLQ via
  visibility-timeout expiry.

Signature-failure log scrubbing (ADR-0017 §"The local poller"): on a
signature failure we log **only** the SQS message ID and the derived
``event_id``. We never log the body, the repository, the PR number, or
any other field from the envelope — a misrouted employer webhook would
otherwise leak metadata into the wrong deployment's CloudWatch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Literal, Protocol

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from treadmill_api.eventbus import EventPublisher
from treadmill_api.events import encode_payload, parse_payload
from treadmill_api.observability import extract_trace_context, get_tracer
from sqlalchemy import func, select
from treadmill_api.models import Event, TaskPR
from treadmill_api.webhooks.inbox_envelope import WebhookInboxEnvelope
from treadmill_api.webhooks.normalize import (
    NormalizationResult,
    normalize_github_event,
)
from treadmill_api.webhooks.pending_events import (
    buffer_pending_event,
    pr_pending_buffer_key,
)
from treadmill_api.webhooks.signatures import (
    InvalidSignatureError,
    SignatureMissingError,
    verify_github_signature_any,
)

HealthStatus = Literal["starting", "running", "degraded", "dead"]
"""Reportable poller states; mirrors ``coordination.consumer.HealthStatus``."""

_MAX_BACKOFF_SECONDS = 30
"""Cap on the exponential backoff between failing polls."""

_FAILURES_BEFORE_ERROR_LOG = 5
"""After this many consecutive failures, escalate from WARNING-ish
``exception`` logs to a distinctive ``error`` log so operators see the
loop is in trouble."""

logger = logging.getLogger("treadmill.webhook_inbox")


class _SecretsManagerClient(Protocol):
    """Structural type for the boto3 Secrets Manager client. The real
    client is sync; we wrap calls in ``asyncio.to_thread`` so the event
    loop never blocks."""

    def get_secret_value(self, SecretId: str) -> dict[str, Any]: ...


def _extract_commit_sha(action: str, body: dict[str, Any]) -> str | None:
    """Pull the HEAD-at-event-time commit SHA from a raw GitHub payload.

    Per ADR-0014 every github event whose semantics are "I happened at a
    specific HEAD" populates ``events.commit_sha`` so ADR-0013's
    ``task_mergeability`` VIEW can join on it without JSONB extraction.

    Duplicated from ``routers/webhooks.py`` for now; both ingress paths
    extract the same fields. Refactoring into a shared helper is an
    invitation for drift — the two callers should agree by code review,
    not by accidental import.
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
        return pr.get("merge_commit_sha") or head.get("sha") or None
    if action == "check_run_completed":
        check_run = body.get("check_run") or {}
        return check_run.get("head_sha") or None
    return None


class WebhookInboxPoller:
    """Background task that drains the webhook-inbox SQS queue.

    Owns no clients directly — all dependencies are injected so tests can
    construct one against a stub SQS, stub Secrets Manager, and stub
    publisher.

    Args:
        sqs_client: boto3 SQS client (sync; we wrap calls in
            ``asyncio.to_thread``). The queue URL is the AWS-side inbox
            populated by the webhook-receiver Lambda.
        queue_url: The webhook-inbox queue URL (per ADR-0017).
        secrets_manager_client: boto3 Secrets Manager client (sync). The
            poller fetches the webhook secret value at ``start()`` time
            and caches it for the poller's lifetime — rotation requires
            an API restart (acceptable trade-off per ADR-0017 §"Webhook
            secret in Secrets Manager").
        webhook_secret_name: The Secrets Manager secret name to fetch
            (e.g., ``treadmill-personal/github-webhook-secret``).
        sessionmaker: Async SQLAlchemy sessionmaker for Event persistence.
        publisher: ``EventPublisher`` (typically ``SNSEventPublisher``)
            for fan-out after successful persist.
        wait_time_seconds: SQS long-poll duration; default 20s (the SQS
            max, recommended for low-volume queues).
        max_messages: SQS receive batch size; default 1 (per ADR-0017
            Q17.d — multi-message batching is a future optimization).
        verifier: Override for ``verify_github_signature`` (test injection
            point). Defaults to the real verifier.
        normalizer: Override for ``normalize_github_event`` (test
            injection point). Defaults to the real normalizer.
        staleness_seconds: How long without a successful poll before the
            health probe reports ``unreachable``. Default 120s, generous
            relative to the 20s long-poll cadence.
        redis_client: Optional async Redis client. When set, the poller
            mirrors the HTTP route's cache-then-heal buffering — a
            task_prs miss on a github event with (repo, pr_number)
            persists the Event with ``task_id=NULL`` *and* buffers the
            event_id on the pending list so the future task_prs INSERT
            drains it (ADR-0063 Step 1). ``None`` skips the buffer call
            (narrow tests, log-only deployments).
    """

    def __init__(
        self,
        sqs_client: Any,
        queue_url: str,
        secrets_manager_client: _SecretsManagerClient,
        webhook_secret_name: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        wait_time_seconds: int = 20,
        max_messages: int = 1,
        verifier: Any = None,
        normalizer: Any = None,
        staleness_seconds: float = 120.0,
        app_webhook_secret: str | None = None,
        redis_client: Any = None,
    ) -> None:
        self.sqs = sqs_client
        self.queue_url = queue_url
        self.secrets_manager = secrets_manager_client
        self.webhook_secret_name = webhook_secret_name
        self.sessionmaker = sessionmaker
        self.publisher = publisher
        self.wait_time_seconds = wait_time_seconds
        self.max_messages = max_messages
        self._verifier = verifier or verify_github_signature_any
        self._normalizer = normalizer or normalize_github_event
        self.staleness_seconds = staleness_seconds
        # Per ADR-0063 Step 1, the SQS ingress mirrors the HTTP route's
        # cache-then-heal buffering: when the task_prs lookup misses, we
        # persist the Event with task_id=NULL AND push the event_id onto
        # the (repo, pr_number) Redis list so the consumer's task_prs
        # back-fill path can drain it once the bridge row appears. The
        # client is optional so narrow tests (and log-only deployments)
        # can construct the poller without Redis wiring.
        self.redis_client = redis_client

        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        self._health_status: HealthStatus = "starting"
        self._webhook_secret: str | None = None
        # ADR-0049 phase 6: the GitHub App webhook secret VALUE, injected by
        # the adapter (host-side fetch, like the App private key) because the
        # API's IAM user cannot read the manually-created secret. When set, the
        # poller verifies each delivery against EITHER this or the legacy
        # secret, so deliveries from the old repo webhook and the App webhook
        # both validate during/after cutover.
        self._app_webhook_secret: str | None = app_webhook_secret
        # ``time.monotonic`` of the last successful poll (raises on the
        # SQS receive_message call do not count). Used by the staleness
        # probe — ``None`` means "no successful poll yet."
        self._last_success_monotonic: float | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Fetch the cached webhook secret + launch the background poll task.

        The Secrets Manager fetch happens here (not in ``__init__``) so
        the call is async-aware and the failure mode is "API fails to
        start" rather than "API runs but never validates signatures."
        Per ADR-0017 §"Webhook secret in Secrets Manager", the secret is
        cached for the poller's lifetime; rotation requires an API
        restart.
        """
        self._stopped = False
        self._health_status = "starting"
        self._webhook_secret = await self._fetch_webhook_secret()
        self._task = asyncio.create_task(
            self._run(), name="webhook-inbox-poller",
        )
        logger.info(
            "webhook inbox poller started: queue=%s secret=%s",
            self.queue_url, self.webhook_secret_name,
        )

    async def stop(self) -> None:
        """Cancel the background task and wait for it to settle.

        Mirrors ``CoordinationConsumer.stop`` — cancellation-safe; an
        in-flight ``receive_message`` long-poll cancels cleanly because
        ``asyncio.to_thread`` propagates ``CancelledError`` into the
        wrapped sync call's join.
        """
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("webhook inbox poller raised on shutdown")
            self._task = None
        logger.info("webhook inbox poller stopped")

    # ── Secret fetch ──────────────────────────────────────────────────────────

    async def _fetch_secret(self, secret_name: str) -> str:
        """Fetch a secret's value from Secrets Manager.

        Wrapped in ``asyncio.to_thread`` so the boto3 sync call doesn't
        block the event loop during startup. Raises on failure so the
        lifespan handler surfaces the misconfiguration.
        """
        resp = await asyncio.to_thread(
            self.secrets_manager.get_secret_value,
            SecretId=secret_name,
        )
        secret = resp.get("SecretString")
        if not secret:
            raise RuntimeError(
                f"Secrets Manager secret {secret_name!r} "
                "has no SecretString value (was it populated?)",
            )
        return secret

    async def _fetch_webhook_secret(self) -> str:
        """Fetch the (legacy) webhook secret. Thin wrapper over ``_fetch_secret``."""
        return await self._fetch_secret(self.webhook_secret_name)

    # ── Poll loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Poll loop with exponential-backoff resilience.

        Per the Phase-3 closure pattern enumerated in ADR-0017: each
        consecutive failing poll sleeps ``min(2 ** (failures - 1), 30)``
        seconds. The counter resets on the first successful poll. After
        ``_FAILURES_BEFORE_ERROR_LOG`` consecutive failures we escalate
        the log message.

        No auto-restart at v0. If this coroutine exits via an unhandled
        exception, the task dies and ``status_for_health`` flips to
        ``dead`` — the readiness probe returns 503 and an operator
        restarts the API. Auto-restart would hide bugs.
        """
        failures = 0
        try:
            while not self._stopped:
                try:
                    resp = await asyncio.to_thread(
                        self.sqs.receive_message,
                        QueueUrl=self.queue_url,
                        MaxNumberOfMessages=self.max_messages,
                        WaitTimeSeconds=self.wait_time_seconds,
                        MessageAttributeNames=["All"],
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failures += 1
                    if failures >= _FAILURES_BEFORE_ERROR_LOG:
                        logger.error(
                            "webhook inbox poll loop has failed %d times in a "
                            "row; poller is degraded — investigate SQS / "
                            "network",
                            failures,
                            exc_info=True,
                        )
                    else:
                        logger.exception(
                            "webhook inbox poll loop error (failure %d)",
                            failures,
                        )
                    self._health_status = "degraded"
                    delay = min(2 ** (failures - 1), _MAX_BACKOFF_SECONDS)
                    await asyncio.sleep(delay)
                    continue

                # Successful poll — reset backoff, mark running, update
                # liveness watermark. Message-handler exceptions are
                # caught inside ``_process`` and never bubble up to flip
                # us back to degraded.
                failures = 0
                self._health_status = "running"
                self._last_success_monotonic = time.monotonic()
                for message in resp.get("Messages", []):
                    await self._process(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._health_status = "dead"
            raise

    # ── Per-message processing ────────────────────────────────────────────────

    async def _process(self, message: dict[str, Any]) -> None:
        """Validate, signature-verify, normalize, persist, publish, delete.

        Every rejection path (malformed envelope, missing delivery header,
        signature failure) deletes the SQS message — re-delivery would
        just fail again, so we prefer poison-safe over DLQ noise.
        """
        message_id = message.get("MessageId", "<unknown>")
        body = message.get("Body", "")

        # 1. Validate the envelope shape.
        try:
            envelope = WebhookInboxEnvelope.model_validate_json(body)
        except PydanticValidationError:
            # Do NOT log the raw body — could carry secrets or PII from a
            # misrouted source. ADR-0017's log-scrubbing requirement.
            logger.warning(
                "webhook inbox: malformed envelope (sqs message_id=%s); "
                "deleting (poison-safe)",
                message_id,
            )
            await self._delete(message)
            return

        # 2. Look up x-github-delivery (case-insensitive; the Lambda
        #    lowercases on its way out, but be defensive).
        delivery = _header_lookup(envelope.headers, "x-github-delivery")
        if not delivery:
            logger.warning(
                "webhook inbox: envelope missing x-github-delivery "
                "(sqs message_id=%s); deleting (poison-safe)",
                message_id,
            )
            await self._delete(message)
            return

        # 3. Derive the deterministic event_id from the GitHub delivery
        #    UUID. ADR-0017 §"Deterministic event_id derivation":
        #    combined with ON CONFLICT DO NOTHING this collapses SQS
        #    visibility-timeout redeliveries onto the same Event row.
        event_id = uuid.uuid5(uuid.NAMESPACE_OID, delivery)

        # 4. Verify the HMAC signature.
        signature = _header_lookup(envelope.headers, "x-hub-signature-256")
        try:
            self._verifier(
                [self._webhook_secret, self._app_webhook_secret],
                envelope.body.encode("utf-8"),
                signature,
            )
        except (SignatureMissingError, InvalidSignatureError):
            # ADR-0017 log-scrubbing: log ONLY the SQS message ID and the
            # derived event_id. Never the body, the repo, the PR number,
            # or any envelope header — a misrouted employer webhook
            # would otherwise leak metadata into the wrong deployment's
            # CloudWatch. The exception type is intentionally omitted
            # from the log message (whether the header was missing vs.
            # mismatched is operationally diagnostic for the *sender*,
            # not for us).
            logger.warning(
                "webhook inbox: signature failed; deleting (poison-safe) "
                "(sqs message_id=%s event_id=%s)",
                message_id, event_id,
            )
            await self._delete(message)
            return

        # 5. Normalize via the same path the HTTP route uses. We need to
        #    parse JSON for the normalizer; if that fails the envelope
        #    body claimed to be a webhook but isn't one — drop poison-safe.
        github_event = _header_lookup(envelope.headers, "x-github-event")
        if not github_event:
            logger.warning(
                "webhook inbox: envelope missing x-github-event "
                "(sqs message_id=%s event_id=%s); deleting (poison-safe)",
                message_id, event_id,
            )
            await self._delete(message)
            return

        try:
            body_json = json.loads(envelope.body)
        except json.JSONDecodeError:
            logger.warning(
                "webhook inbox: envelope body is not valid JSON "
                "(sqs message_id=%s event_id=%s); deleting (poison-safe)",
                message_id, event_id,
            )
            await self._delete(message)
            return

        normalized = self._normalizer(github_event, body_json)
        if normalized is None:
            # Unhandled event type (push, issues, ping, etc.) — the HTTP
            # route returns 200 + status=skipped today; the poller's
            # equivalent is to acknowledge the SQS message without a
            # DB write or publish. Log at INFO so the operator can see
            # what GitHub is sending that we don't process.
            logger.info(
                "webhook inbox: skipping unhandled github event "
                "(sqs message_id=%s event_id=%s github_event=%s)",
                message_id, event_id, github_event,
            )
            await self._delete(message)
            return

        # 6. Persist Event row + publish + delete.
        trace_ctx = extract_trace_context(message.get("MessageAttributes", {}))
        tracer = get_tracer("treadmill.webhook_inbox")
        with tracer.start_as_current_span(
            "treadmill.webhook_inbox.process", context=trace_ctx,
        ):
            try:
                await self._persist_and_publish(
                    event_id=event_id,
                    normalized=normalized,
                    body_json=body_json,
                )
            except Exception:
                # Per ADR-0017 §"The local poller", on persistence/publish
                # failure we leave the SQS message visible (it will retry
                # after the visibility timeout). The handler logs but does
                # NOT delete — mirrors the consumer's "transient errors
                # bubble back to the queue" contract.
                logger.exception(
                    "webhook inbox: persist/publish failed; leaving message "
                    "for SQS retry (sqs message_id=%s event_id=%s)",
                    message_id, event_id,
                )
                return

            await self._delete(message)

    async def _persist_and_publish(
        self,
        *,
        event_id: uuid.UUID,
        normalized: NormalizationResult,
        body_json: dict[str, Any],
    ) -> None:
        """Persist the Event row idempotently, then publish.

        The INSERT uses ``ON CONFLICT (id) DO NOTHING`` against the
        deterministic ``event_id``. If a re-delivery lands the second
        INSERT is a no-op; we still publish — the bus is notification
        only, and the consumer's downstream idempotency (also keyed on
        event_id) collapses duplicates.

        Per ADR-0014, ``commit_sha`` is populated for every github event
        with a commit anchor so the mergeability VIEW can join without
        JSONB extraction. The HTTP route does the same.
        """
        # Validate the normalized payload via the typed registry. A
        # failure here means the normalizer drifted from the registry —
        # propagate so the SQS message stays for retry (the operator
        # restarts the API after fixing the bug).
        typed = parse_payload(
            normalized.entity_type, normalized.action, normalized.payload,
        )

        commit_sha = _extract_commit_sha(normalized.action, body_json)

        async with self.sessionmaker() as session:
            # Resolve task_id via the task_prs bridge — mirror of the
            # HTTP route at ``routers/webhooks.py:193-202``. The two
            # ingress paths must stay in lock-step on this field; the
            # dependency gate ``dispatch._is_dep_pr_merged`` queries
            # ``events.task_id`` directly and a NULL here silently
            # blocks downstream task dispatch. See the dual-ingress
            # drift learning at 2026-05-14.
            task_id: uuid.UUID | None = None
            if normalized.repo and normalized.pr_number is not None:
                result = await session.execute(
                    select(TaskPR.task_id).where(
                        func.lower(TaskPR.repo) == normalized.repo.lower(),
                        TaskPR.pr_number == normalized.pr_number,
                    )
                )
                task_id = result.scalar_one_or_none()

            stmt = (
                pg_insert(Event)
                .values(
                    id=event_id,
                    entity_type=normalized.entity_type,
                    action=normalized.action,
                    task_id=task_id,
                    payload=encode_payload(typed),
                    commit_sha=commit_sha,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.execute(stmt)
            await session.commit()

            # ADR-0063 Step 1 — mirror of the HTTP route's cache-then-heal
            # buffer at ``routers/webhooks.py:223-240``. When the task_prs
            # lookup missed but the event carries a (repo, pr_number) pair,
            # push the event_id onto the pending list so the consumer's
            # task_prs back-fill path (``_write_task_prs_on_completed`` +
            # ``_try_task_prs_fallback_on_pr_merged``) drains it once the
            # bridge row eventually appears. The buffer call is best-effort
            # — the row is persisted; a Redis hiccup degrades the race
            # window but never loses audit data. Guarded by
            # ``redis_client is not None`` so deployments / narrow tests
            # that omit Redis wiring don't crash the ingest path.
            if (
                task_id is None
                and normalized.repo
                and normalized.pr_number is not None
                and self.redis_client is not None
            ):
                try:
                    await buffer_pending_event(
                        self.redis_client,
                        pr_pending_buffer_key(
                            normalized.repo, normalized.pr_number,
                        ),
                        event_id,
                    )
                    logger.info(
                        "webhook inbox: buffered pending event_id=%s for "
                        "repo=%s pr=%d (task_prs miss; awaiting bridge row "
                        "to drain)",
                        event_id, normalized.repo, normalized.pr_number,
                    )
                except Exception:
                    logger.exception(
                        "webhook inbox: pending-event buffering failed for "
                        "event_id=%s repo=%s pr=%d; row is persisted",
                        event_id, normalized.repo, normalized.pr_number,
                    )

            # Re-fetch the row so the publisher sees the canonical Event
            # (the INSERT may have been a no-op on re-delivery; we want
            # the existing row's fields, not the proposed ones, so the
            # publish carries the audit-log-of-record values).
            event = await session.get(Event, event_id)

        if event is None:
            # Should never happen — the row was either inserted by us or
            # already existed. Log loudly and bail.
            logger.error(
                "webhook inbox: Event row %s vanished after upsert; "
                "skipping publish",
                event_id,
            )
            return

        try:
            await self.publisher.publish(event, typed)
        except Exception:
            # Publish-failure semantics: log + swallow. The Event row is
            # persisted (source of truth); the bus is notification only.
            # The replay loop heals dispatch-publish failures, but the
            # webhook ingest path doesn't write a dispatch marker — the
            # downstream consumer's poll-driven rescan picks up the row
            # by entity_type/action index (which is what bunkhouse does).
            logger.exception(
                "webhook inbox: event publish failed for event_id=%s; "
                "row is persisted, consumer rescan will pick it up",
                event_id,
            )

    async def _delete(self, message: dict[str, Any]) -> None:
        receipt = message.get("ReceiptHandle")
        if not receipt:
            logger.warning(
                "webhook inbox: SQS message missing ReceiptHandle; skipping delete "
                "(sqs message_id=%s)",
                message.get("MessageId"),
            )
            return
        await asyncio.to_thread(
            self.sqs.delete_message,
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt,
        )

    # ── Probe helpers ─────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """Whether the background poll task is alive.

        Used by ``WebhookInboxProbe`` to report poller health on
        ``/health/ready``. ``True`` iff a task has been started AND it
        has not finished. Mirrors ``CoordinationConsumer.is_running``.
        """
        return self._task is not None and not self._task.done()

    def status_for_health(self) -> HealthStatus:
        """Granular health label for the poller.

        One of ``starting`` / ``running`` / ``degraded`` / ``dead``. Per
        Phase-3 closure A.11 there is no auto-restart at v0: ``dead`` is
        terminal until the API process is restarted.
        """
        if self._task is not None and self._task.done():
            if not self._stopped:
                return "dead"
        return self._health_status

    def is_stale(self) -> bool:
        """Whether the last successful poll is older than the staleness
        threshold.

        ``True`` when no poll has succeeded yet AND the poller has been
        up long enough that one should have (we approximate this by
        treating "no successful poll yet" as not-stale; staleness only
        applies after at least one success has set the watermark). After
        the first success, ``True`` iff ``now - last_success >
        staleness_seconds``.

        The probe uses this in addition to ``is_running`` so a poller
        whose long-poll-thread hangs (but doesn't raise) still flips the
        probe.
        """
        if self._last_success_monotonic is None:
            return False
        return (
            time.monotonic() - self._last_success_monotonic
            > self.staleness_seconds
        )


def _header_lookup(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup.

    The Lambda lowercases header names on its way out per its
    ``PRESERVE_HEADERS`` contract, so direct dict access ``headers[name]``
    works in the happy path. Defensive lookup here in case a future
    Lambda revision (or a manually-crafted test envelope) ships
    mixed-case keys.
    """
    needle = name.lower()
    for key, value in headers.items():
        if key.lower() == needle:
            return value
    return None
