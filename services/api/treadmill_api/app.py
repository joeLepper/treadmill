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
    CoordinationProbe,
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
from treadmill_api.routers.event_triggers import router as event_triggers_router
from treadmill_api.routers.github import router as github_router
from treadmill_api.routers.hooks import router as hooks_router
from treadmill_api.routers.onboarding import router as onboarding_router
from treadmill_api.routers.plans import router as plans_router
from treadmill_api.routers.roles import router as roles_router
from treadmill_api.routers.schedules import router as schedules_router
from treadmill_api.routers.skills import router as skills_router
from treadmill_api.routers.steps import router as steps_router
from treadmill_api.routers.tasks import router as tasks_router
from treadmill_api.routers.webhooks import router as webhooks_router
from treadmill_api.routers.workflow_triggers import (
    router as workflow_triggers_router,
)
from treadmill_api.routers.workflows import router as workflows_router

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
        CoordinationConsumer,
        ReplayLoop,
        WebhookInboxPoller,
    )
    from treadmill_api.dispatch import Dispatcher
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
    github_clients = build_github_clients(settings)
    github_client = github_clients.client
    if github_client is None:
        logger.warning(
            "no GitHub auth configured (GITHUB_APP_* / GITHUB_TOKEN unset); "
            "conflict-detection sweep + merge on pr_merged will be skipped "
            "(ADR-0013 / ADR-0049)"
        )

    publisher = make_publisher(settings, sns_client)
    set_publisher(publisher)

    # Coordination consumer — only started when the events queue is wired
    # AND the engine exists (the consumer is the sole writer of step
    # status; without a DB, there's nothing for it to do).
    consumer: CoordinationConsumer | None = None
    replay_loop: ReplayLoop | None = None
    if (
        settings.events_queue_url is not None
        and sqs_client is not None
        and engine is not None
    ):
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        # Construct a background-callable Dispatcher for the consumer's
        # re-evaluation pass (D.6). It shares the same publisher + SQS
        # client as the HTTP-path dispatcher so dispatched runs travel
        # the same publish path.
        bg_dispatcher = Dispatcher(
            publisher=publisher,
            sqs_client=sqs_client,
            work_queue_url=settings.work_queue_url,
        )
        consumer = CoordinationConsumer(
            sqs_client=sqs_client,
            queue_url=settings.events_queue_url,
            sessionmaker=sessionmaker,
            redis_client=redis,
            publisher=publisher,
            dispatcher=bg_dispatcher,
            github_client=github_client,
            settings=settings,
        )
        await consumer.start()
        # Replay loop heals dispatch-publish failures (A.8/A.10). Shares
        # the same sessionmaker as the consumer; uses the configured
        # publisher so a healed re-issue goes through the same SNS path
        # as the original dispatcher attempt.
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
        # The consumer block above already built a sessionmaker iff its
        # own preconditions were met. The poller can start independently
        # (it doesn't depend on the events queue) so we build one here if
        # the consumer block didn't.
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
        )
        await webhook_inbox_poller.start()

    app.state.settings = settings
    app.state.engine = engine
    app.state.redis = redis
    app.state.sns_client = sns_client
    app.state.sqs_client = sqs_client
    app.state.github_client = github_client
    app.state.publisher = publisher
    app.state.consumer = consumer
    app.state.replay_loop = replay_loop
    app.state.webhook_inbox_poller = webhook_inbox_poller
    app.state.probes = _build_probes(
        engine, redis, consumer, webhook_inbox_poller,
    )

    logger.info(
        "Treadmill API ready (postgres=%s, redis=%s, events_topic=%s, "
        "consumer=%s, webhook_inbox_poller=%s)",
        "wired" if engine is not None else "unconfigured",
        "wired" if redis is not None else "unconfigured",
        "wired" if settings.events_topic_arn else "log-fallback",
        "running" if consumer is not None else "unconfigured",
        "running" if webhook_inbox_poller is not None else "unconfigured",
    )

    tracer = get_tracer("treadmill.api.startup")
    with tracer.start_as_current_span("treadmill.api.startup"):
        try:
            yield
        finally:
            # Replay loop stops first — it shares the sessionmaker with the
            # consumer, and we want it idle before the consumer (and engine)
            # tear down so an in-flight tick can't double-write on shutdown.
            if replay_loop is not None:
                await replay_loop.stop()
            if consumer is not None:
                await consumer.stop()
            if webhook_inbox_poller is not None:
                await webhook_inbox_poller.stop()
            await github_clients.aclose()
            if engine is not None:
                await engine.dispose()
            if redis is not None:
                await redis.aclose()
            logger.info("Treadmill API shut down")


def _build_probes(
    engine, redis, consumer=None, webhook_inbox_poller=None,
) -> list[DependencyProbe]:
    """Construct the readiness-probe list from the wired clients.

    The ``CoordinationProbe`` only joins the list when a consumer was
    actually constructed (env vars set, engine wired). Without one,
    skipping the probe keeps ``/health/ready`` honest — the consumer is
    not configured, so nothing to check. Same pattern for the
    ``WebhookInboxProbe`` per ADR-0017.
    """
    probes: list[DependencyProbe] = [PostgresProbe(engine), RedisProbe(redis)]
    if consumer is not None:
        probes.append(CoordinationProbe(consumer))
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
    app.include_router(steps_router)
    app.include_router(workflows_router)
    app.include_router(workflow_triggers_router)
    app.include_router(roles_router)
    app.include_router(schedules_router)
    app.include_router(skills_router)
    app.include_router(hooks_router)
    app.include_router(event_triggers_router)
    app.include_router(github_router)
    app.include_router(webhooks_router)
    app.include_router(onboarding_router)
    app.include_router(context_docs_router)
    app.include_router(claude_credentials_router)
    app.include_router(dashboard_router)
    return app


# Default app instance for ASGI servers (uvicorn, etc.).
app = create_app()
