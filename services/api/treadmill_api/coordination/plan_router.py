"""PlanRouter — routing decisions after a step projection commits.

Extracted from ``coordination/consumer.py`` per ADR-0084 §"Phase 3C" /
Task 2A Phase 2. The router owns every routing decision that consumes
the *committed* projection: every ``_maybe_*`` workflow-firing helper,
the cross-step dispatcher, the re-evaluation pass, the four
entity-type-specific event handlers (plan, github, schedule,
task-architect-emit-failure, task-terminal-for-triage), and the D.8
webhook-event drain.

Session ownership
-----------------
The router owns its own ``async_sessionmaker`` and opens a SEPARATE
session per ``route_*`` call. This is load-bearing for the auto-merge
race documented at the old ``consumer.py:559-569``: routing helpers
read the ``task_mergeability`` VIEW (and other event-projection
VIEWs), and the VIEW projects from the ``events`` table. The
``CoordinationConsumer`` commits the projection transaction BEFORE
delegating to the router, so the router's VIEW reads see the
just-committed state — same invariant the pre-extraction code relied
on via an explicit ``session.flush()`` then a continuation of the same
transaction.

Two transactions, one logical handler invocation. The projection
commits first (the audit row is the source of truth and must survive
even if routing crashes). The router opens its own session, runs its
decisions, and commits its own writes (dispatched-run audit rows,
internal Event INSERTs like ``review.override``, ``task_prs`` follow-
ons via ``_cross_step_dispatch``).

This file is a placeholder skeleton landing in the same PR as the
trace-replay equivalence harness. The 21 ``_maybe_*`` helpers,
``_cross_step_dispatch``, ``_reevaluate``, and the four ``_handle_*``
event-type handlers move from ``CoordinationConsumer`` to this class
across follow-up commits in this PR. The trace-replay harness gates
the merge: every extracted method's behavior is asserted equivalent
against a 1453-event captured trace before the PR lands.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("treadmill.coordination.router")


class PlanRouter:
    """Owns routing decisions that consume the committed event projection.

    Constructed once at lifespan-startup alongside ``CoordinationConsumer``.
    The consumer's ``handle()`` calls ``route_step()`` (or one of the
    entity-type-specific ``route_*`` methods) AFTER committing the
    projection transaction.

    Holds the same optional clients ``CoordinationConsumer`` does
    today — they're ``None`` in narrow tests, and each helper short-
    circuits cleanly when its required client is missing.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        redis_client: Any | None = None,
        publisher: Any | None = None,
        dispatcher: Any | None = None,
        github_client: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.redis_client = redis_client
        self.publisher = publisher
        self.dispatcher = dispatcher
        self.github_client = github_client
        self.settings = settings

    async def route_step(
        self,
        record: dict[str, Any],
        *,
        typed: Any,
        action: str | None,
        step_id: str,
    ) -> None:
        """Run every routing decision triggered by a step.* event.

        Placeholder. The extracted ``_maybe_*`` helpers + ``_cross_step_dispatch``
        + ``_reevaluate`` invocation move here across follow-up commits in
        this PR.
        """
        raise NotImplementedError(
            "PlanRouter.route_step is a Phase-2 skeleton; "
            "follow-up commits in this PR move the extracted routing "
            "methods here. The trace-replay harness gates the merge."
        )
