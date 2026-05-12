"""Dependency probes for the readiness endpoint.

A probe asks one external dependency: *are you reachable right now?* The
result is one of three states — ``ok`` (reachable), ``unreachable``
(configured but not responding), ``not_configured`` (no URL was set, so
no probe to run).

The readiness endpoint composes probes and reports per-dependency status.
A single ``unreachable`` flips the overall status to 503.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import redis.asyncio as redis_async
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from treadmill_api.coordination import CoordinationConsumer
    from treadmill_api.coordination.webhook_inbox import WebhookInboxPoller


class ProbeStatus(StrEnum):
    OK = "ok"
    UNREACHABLE = "unreachable"
    NOT_CONFIGURED = "not_configured"


@dataclass
class ProbeResult:
    name: str
    status: ProbeStatus
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        body: dict[str, str] = {"status": self.status.value}
        if self.detail is not None:
            body["detail"] = self.detail
        return body


class DependencyProbe(Protocol):
    name: str

    async def check(self) -> ProbeResult: ...


class PostgresProbe:
    """Probes Postgres reachability via ``SELECT 1`` against the engine."""

    name = "postgres"

    def __init__(self, engine: AsyncEngine | None) -> None:
        self.engine = engine

    async def check(self) -> ProbeResult:
        if self.engine is None:
            return ProbeResult(self.name, ProbeStatus.NOT_CONFIGURED)
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return ProbeResult(self.name, ProbeStatus.OK)
        except Exception as exc:
            return ProbeResult(self.name, ProbeStatus.UNREACHABLE, detail=str(exc))


class RedisProbe:
    """Probes Redis reachability via ``PING``."""

    name = "redis"

    def __init__(self, client: redis_async.Redis | None) -> None:
        self.client = client

    async def check(self) -> ProbeResult:
        if self.client is None:
            return ProbeResult(self.name, ProbeStatus.NOT_CONFIGURED)
        try:
            pong = await self.client.ping()
            if not pong:
                return ProbeResult(
                    self.name, ProbeStatus.UNREACHABLE, detail="ping returned falsy"
                )
            return ProbeResult(self.name, ProbeStatus.OK)
        except Exception as exc:
            return ProbeResult(self.name, ProbeStatus.UNREACHABLE, detail=str(exc))


class CoordinationProbe:
    """Probes the coordination consumer's poll task.

    Per the 2026-05-11 closure plan (C.6), the consumer is the *only*
    writer of ``workflow_run_steps.status``; if its background task has
    died, the API is silently broken — events keep arriving on SQS but
    nothing projects them. The probe surfaces that as readiness failure
    so the load balancer pulls the API out of rotation until it's healed.

    Status mapping:
      * consumer is None        → ``not_configured`` (env vars unset)
      * task alive              → ``ok``
      * task done or never set  → ``unreachable``
    """

    name = "coordination_consumer"

    def __init__(self, consumer: "CoordinationConsumer | None") -> None:
        self.consumer = consumer

    async def check(self) -> ProbeResult:
        if self.consumer is None:
            return ProbeResult(self.name, ProbeStatus.NOT_CONFIGURED)
        if self.consumer.is_running():
            return ProbeResult(self.name, ProbeStatus.OK)
        return ProbeResult(
            self.name, ProbeStatus.UNREACHABLE, detail="consumer task is not running",
        )


class WebhookInboxProbe:
    """Probes the webhook-inbox poller's background task.

    Sibling to ``CoordinationProbe`` for the AWS-side webhook ingest
    path (ADR-0017). When the poller dies — SQS credentials expired,
    Secrets Manager unreachable on startup, unhandled exception in the
    poll loop — the API has silently lost a load-bearing capability:
    GitHub webhooks keep arriving on SQS but nothing projects them.
    Flipping ``/healthz`` to 503 makes the failure visible.

    Status mapping:
      * poller is None              → ``not_configured`` (env vars unset
        or mode is fully_local)
      * task alive AND not stale    → ``ok``
      * task alive but poll watermark is older than the staleness
        threshold → ``unreachable`` (the long-poll thread may be wedged)
      * task done or never set      → ``unreachable``
    """

    name = "webhook_inbox_poller"

    def __init__(self, poller: "WebhookInboxPoller | None") -> None:
        self.poller = poller

    async def check(self) -> ProbeResult:
        if self.poller is None:
            return ProbeResult(self.name, ProbeStatus.NOT_CONFIGURED)
        if not self.poller.is_running():
            return ProbeResult(
                self.name, ProbeStatus.UNREACHABLE,
                detail="webhook inbox poller task is not running",
            )
        if self.poller.is_stale():
            return ProbeResult(
                self.name, ProbeStatus.UNREACHABLE,
                detail=(
                    f"no successful SQS poll in "
                    f"{self.poller.staleness_seconds:.0f}s"
                ),
            )
        return ProbeResult(self.name, ProbeStatus.OK)


async def run_probes(probes: list[DependencyProbe]) -> list[ProbeResult]:
    """Run each probe sequentially. Errors inside probes are caught at the
    probe level; this function does not raise."""
    results: list[ProbeResult] = []
    for probe in probes:
        results.append(await probe.check())
    return results


def overall_status(results: list[ProbeResult]) -> ProbeStatus:
    """``unreachable`` if any probe is unreachable; ``ok`` otherwise.

    ``not_configured`` does not flip overall status — a probe that isn't
    wired is not a readiness failure (production deploys wire all deps;
    local dev may run without some).
    """
    if any(r.status is ProbeStatus.UNREACHABLE for r in results):
        return ProbeStatus.UNREACHABLE
    return ProbeStatus.OK
