"""Treadmill API — FastAPI application factory.

Per ADR-0011, this service is event-driven (publishes/consumes events via
the SNS+SQS substrate provisioned by ADR-0002) and persists state in
append-only form (Postgres VIEW computes derived status).

Day 1B wires the SQLAlchemy async engine, Redis async client, and the
readiness-probe list into ``app.state`` via the FastAPI lifespan handler.
Subsequent days add the routers and event publisher.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from treadmill_api import __version__
from treadmill_api.cache import make_redis
from treadmill_api.config import DeploymentMode, Settings, get_settings
from treadmill_api.database import make_engine
from treadmill_api.github_auth import build_github_clients
from treadmill_api.dependencies import (
    DependencyProbe,
    PostgresProbe,
    RedisProbe,
    WebhookInboxProbe,
)
from treadmill_api.observability import get_tracer
from treadmill_api.health import router as health_router
from treadmill_api.routers.claude_credentials import router as claude_credentials_router
from treadmill_api.routers.context_docs import router as context_docs_router
from treadmill_api.routers.dashboard import router as dashboard_router
from treadmill_api.routers.escalations import router as escalations_router
from treadmill_api.routers.events import router as events_router
from treadmill_api.routers.github import router as github_router
from treadmill_api.routers.onboarding import router as onboarding_router
from treadmill_api.routers.plans import router as plans_router
from treadmill_api.routers.schedules import router as schedules_router
from treadmill_api.routers.system_status import router as system_status_router
from treadmill_api.routers.task_board import router as task_board_router
from treadmill_api.routers.task_executions import router as task_executions_router
from treadmill_api.routers.tasks import router as tasks_router
from treadmill_api.routers.llm_calls import router as llm_calls_router
from treadmill_api.routers.team_configs import router as team_configs_router
from treadmill_api.routers.webhooks import router as webhooks_router
from treadmill_api.routers.task_prs import router as task_prs_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Construct + register dependency clients at startup; dispose at shutdown.

    Clients are created lazily — engine and redis return ``None`` when their
    URL is unset, so the API can boot for healthcheck-only inspection
    without a database or cache.
    """
    import boto3
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from treadmill_api.coordination import (
        NotificationFanout,
        ReplayLoop,
        WebhookInboxPoller,
        make_notification_fanout,
    )
    from treadmill_api.eventbus import make_publisher, set_publisher

    settings: Settings = get_settings()

    engine = make_engine(settings)
    redis = make_redis(settings)

    # SNS client for the event bus. boto3 reads AWS_ENDPOINT_URL from env
    # so the same client points at moto locally and real SNS in AWS.
    sns_client = None
    if settings.events_topic_arn:
        sns_client = boto3.client("sns", region_name=settings.aws_region)
    sqs_client = None
    if (
        settings.events_queue_url
        or settings.work_queue_url
        or settings.webhook_inbox_queue_url
    ):
        sqs_client = boto3.client("sqs", region_name=settings.aws_region)

    # GitHub client for merge / PR / conflict-sweep calls. Per ADR-0049,
    # ``build_github_clients`` returns the GitHub App per-repo
    # installation-token client when the App is configured
    # (``GITHUB_APP_ID`` + ``GITHUB_APP_PRIVATE_KEY``), else the legacy
    # static-PAT client, else ``None`` (handlers short-circuit cleanly).
    # Pass the redis client so the App-path token cache is Redis-backed —
    # tokens then survive API recreates and are shared fleet-wide, collapsing
    # GitHub mint volume to ~1/installation/hour (2026-06-04 durable fix).
    # Falls back to the in-process cache when redis is None.
    github_clients = build_github_clients(settings, redis_client=redis)
    github_client = github_clients.client
    if github_client is None:
        logger.warning(
            "no GitHub auth configured (GITHUB_APP_* / GITHUB_TOKEN unset); "
            "conflict-detection sweep + merge on pr_merged will be skipped "
            "(ADR-0013 / ADR-0049)"
        )

    publisher = make_publisher(settings, sns_client)
    set_publisher(publisher)

    # CoordinationConsumer removed per ADR-0087 Phase 4 — the step-event
    # SQS pipeline projected worker step.* events onto workflow_run_steps;
    # both the events and the table are gone. Coordinators write
    # task_executions over HTTP (single-writer per ADR-0087 §Decision).
    #
    # Replay loop heals persist_and_publish SNS failures (A.8/A.10) — it
    # predates and survives the consumer; it needs only a sessionmaker +
    # the publisher.
    replay_loop: ReplayLoop | None = None
    if engine is not None:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        replay_loop = ReplayLoop(
            publisher=publisher,
            sessionmaker=sessionmaker,
        )
        await replay_loop.start()

    # Webhook-inbox poller (ADR-0017). Started in dev_local / fully_remote
    # when the inbox queue + the Secrets Manager secret name are both
    # configured. Skipped in fully_local (no AWS inbox to drain — webhooks
    # arrive directly via POST /api/v1/webhooks/github).
    webhook_inbox_poller: WebhookInboxPoller | None = None
    if (
        settings.deployment_mode
        in {DeploymentMode.DEV_LOCAL, DeploymentMode.FULLY_REMOTE}
        and settings.webhook_inbox_queue_url is not None
        and settings.github_webhook_secret_name is not None
        and engine is not None
        and sqs_client is not None
    ):
        # The replay-loop block above already built a sessionmaker iff
        # the engine exists. The poller can start independently so we
        # build one here if that block didn't.
        try:
            poller_sessionmaker = sessionmaker  # type: ignore[has-type]
        except NameError:
            poller_sessionmaker = async_sessionmaker(
                engine, expire_on_commit=False,
            )
        secrets_client = boto3.client(
            "secretsmanager", region_name=settings.aws_region,
        )
        webhook_inbox_poller = WebhookInboxPoller(
            sqs_client=sqs_client,
            queue_url=settings.webhook_inbox_queue_url,
            secrets_manager_client=secrets_client,
            webhook_secret_name=settings.github_webhook_secret_name,
            app_webhook_secret=settings.github_app_webhook_secret,
            sessionmaker=poller_sessionmaker,
            publisher=publisher,
            redis_client=redis,
        )
        await webhook_inbox_poller.start()

    # Notification fan-out (ADR-0062 Step 4). Subscribes to the in-process
    # eventbus broadcaster and POSTs ``task.escalated_to_operator`` +
    # ``task.escalation_closed`` events to the configured webhook targets
    # (Slack + arbitrary raw-event-JSON URLs). ``start()`` is a no-op when
    # neither ``TREADMILL_SLACK_WEBHOOK_URL`` nor
    # ``TREADMILL_NOTIFICATION_WEBHOOKS`` is set.
    notification_fanout: NotificationFanout = make_notification_fanout(settings)
    await notification_fanout.start()

    app.state.settings = settings
    app.state.engine = engine
    app.state.redis = redis
    app.state.sns_client = sns_client
    app.state.sqs_client = sqs_client
    app.state.github_client = github_client
    # The App-path token cache (None on PAT / no-auth). The
    # /installation-token route mints through this instead of re-minting raw
    # per call — caching ~1h tokens collapses the fleet's busiest GitHub call
    # to roughly one mint per installation per refresh window (2026-06-04 fix).
    app.state.installation_token_cache = github_clients.installation_cache
    app.state.publisher = publisher
    app.state.replay_loop = replay_loop
    app.state.webhook_inbox_poller = webhook_inbox_poller
    app.state.notification_fanout = notification_fanout
    app.state.probes = _build_probes(
        engine, redis, webhook_inbox_poller=webhook_inbox_poller,
    )

    logger.info(
        "Treadmill API ready (postgres=%s, redis=%s, events_topic=%s, "
        "webhook_inbox_poller=%s)",
        "wired" if engine is not None else "unconfigured",
        "wired" if redis is not None else "unconfigured",
        "wired" if settings.events_topic_arn else "log-fallback",
        "running" if webhook_inbox_poller is not None else "unconfigured",
    )

    tracer = get_tracer("treadmill.api.startup")
    with tracer.start_as_current_span("treadmill.api.startup"):
        try:
            yield
        finally:
            # Replay loop stops first so an in-flight tick can't
            # double-write while the engine tears down.
            if replay_loop is not None:
                await replay_loop.stop()
            if webhook_inbox_poller is not None:
                await webhook_inbox_poller.stop()
            await notification_fanout.stop()
            await github_clients.aclose()
            if engine is not None:
                await engine.dispose()
            if redis is not None:
                await redis.aclose()
            logger.info("Treadmill API shut down")


def _build_probes(
    engine, redis, webhook_inbox_poller=None,
) -> list[DependencyProbe]:
    """Construct the readiness-probe list from the wired clients.

    The ``WebhookInboxProbe`` only joins the list when the poller was
    actually constructed (env vars set, engine wired) — skipping it
    keeps ``/health/ready`` honest per ADR-0017. (The old
    ``CoordinationProbe`` left with the step-event consumer in
    ADR-0087 Phase 4.)
    """
    probes: list[DependencyProbe] = [PostgresProbe(engine), RedisProbe(redis)]
    if webhook_inbox_poller is not None:
        probes.append(WebhookInboxProbe(webhook_inbox_poller))
    return probes


def create_app() -> FastAPI:
    """FastAPI application factory.

    Kept as a function (not a module-level instance) so tests can construct
    fresh apps with overridden dependencies. Tests can also bypass the
    lifespan handler by setting ``app.state.probes`` directly.
    """
    app = FastAPI(
        title="Treadmill API",
        description="Event-driven, immutable runtime per ADR-0011",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(plans_router)
    app.include_router(tasks_router)
    app.include_router(task_board_router)
    app.include_router(task_executions_router)
    app.include_router(llm_calls_router)
    app.include_router(team_configs_router)
    app.include_router(task_prs_router)
    app.include_router(schedules_router)
    app.include_router(system_status_router)
    app.include_router(events_router)
    app.include_router(github_router)
    app.include_router(webhooks_router)
    app.include_router(onboarding_router)
    app.include_router(context_docs_router)
    app.include_router(claude_credentials_router)
    app.include_router(dashboard_router)
    app.include_router(escalations_router)
    return app


# Default app instance for ASGI servers (uvicorn, etc.).
app = create_app()
