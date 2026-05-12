"""Treadmill API runner — `treadmill-api` console script entrypoint.

Starts uvicorn against the FastAPI app on the configured port. Production
deploys typically run uvicorn directly via the ECS task command; this
entrypoint exists for local invocation.

Two startup responsibilities live here (and *only* here — so test paths
that bypass ``run()`` are unaffected):

1. ``logging.basicConfig`` — without it, Python's root logger sits at
   WARNING and every ``logger.info(...)`` in ``treadmill_api.*`` is
   silently dropped (Week 4 dev-local deploy friction point #2).
2. ``alembic upgrade head`` — invoked programmatically (no shell-out) so
   a fresh Postgres comes up schema-ready on first deploy (Week 4
   friction point #1). The container's WORKDIR is ``/app`` and
   ``alembic.ini`` is copied there, so the relative path resolves. If
   alembic fails, we fail-fast (let the exception propagate) — a
   schema-misaligned API serving traffic is worse than crash-looping.
"""

from __future__ import annotations

import logging
import time

import uvicorn

from treadmill_api.config import Settings, get_settings


def _run_migrations(settings: Settings) -> None:
    """Run ``alembic upgrade head`` in-process.

    Imports alembic lazily so test paths that don't touch ``run()`` don't
    pay the import cost (and so the alembic-config / env.py side effects
    only fire when migrations actually run).

    Path resolution: ``Config("alembic.ini")`` is relative to the current
    working directory. The Dockerfile sets WORKDIR=/app and copies
    ``alembic.ini`` there, so the lookup succeeds in the container. Local
    invocations must run from ``services/api/`` for the same reason.

    Cold-start retry: when ``treadmill-local up`` starts containers, the
    API may race Postgres readiness — observed during the first dev-local
    cycle. We retry connection failures for up to ~30s before giving up.
    Schema errors (the after-connection class) still fail-fast.
    """
    if settings.skip_migrations:
        logging.getLogger(__name__).info(
            "TREADMILL_SKIP_MIGRATIONS=true — skipping alembic upgrade",
        )
        return

    from alembic import command as alembic_command
    from alembic.config import Config
    from sqlalchemy.exc import OperationalError

    logger = logging.getLogger(__name__)
    logger.info("Running alembic upgrade head")
    cfg = Config("alembic.ini")
    # env.py reads DATABASE_URL from get_settings(), which the container
    # already has wired via env. No need to pass URL through cfg here.

    deadline = time.monotonic() + 30.0
    delay = 0.5
    while True:
        try:
            alembic_command.upgrade(cfg, "head")
            return
        except OperationalError as exc:
            # asyncpg / psycopg surface "connection refused" / "could not
            # connect" as OperationalError. Don't retry schema errors —
            # those are a different exception class (ProgrammingError /
            # InternalError) and indicate real failures.
            if time.monotonic() >= deadline:
                logger.error(
                    "alembic upgrade gave up after 30s; Postgres unreachable: %s",
                    exc,
                )
                raise
            logger.info(
                "Postgres not ready (%s); retrying in %.1fs", exc, delay,
            )
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)


def run() -> None:
    settings = get_settings()

    # Configure the root logger so ``treadmill_api.*`` INFO surfaces in
    # stdout. Uvicorn applies its own log_config separately for access /
    # error loggers; we deliberately do not touch those.
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    _run_migrations(settings)

    uvicorn.run(
        "treadmill_api.app:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
