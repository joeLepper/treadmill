"""Seed the single system Plan used by the scheduler's synthetic-task path.

Per ADR-0057, every scheduled tick (and every operator-trigger) creates a
synthetic ``Task`` tied to a single "system: scheduler" ``Plan`` instead of
using the taskless dispatch path (``task_id = NULL``).

Sentinel UUID: ``00000000-0000-0000-0000-000000000001`` — stable across
deployments so callers can reference it without a runtime lookup.

Two paths:

  * ``seed_system_plan_if_absent(session, *, repo)`` — direct DB INSERT at
    API startup. Inserts the Plan + an ``activated`` event when the row is
    absent. Returns ``True`` if inserted, ``False`` if already present.

  * ``seed_system_plan(api_client, *, repo)`` — HTTP-driven; uses
    ``GET /api/v1/plans/{id}`` to check existence. The HTTP path can only
    verify existence (the plans POST endpoint does not accept a custom ``id``);
    creation requires the DB-direct path or an API restart that triggers
    auto-seed via ``seed_starters_if_empty``.

Both paths are idempotent: re-running is safe.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

logger = logging.getLogger("treadmill.api.seed.system_plan")

SYSTEM_PLAN_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SYSTEM_PLAN_TITLE: str = "system: scheduler"


# ── DB-direct seed path ───────────────────────────────────────────────────────


def seed_system_plan_if_absent(
    session: Any,
    *,
    repo: str | None = None,
) -> bool:
    """Insert the system Plan (+ activated event) if not yet present.

    Called from ``seed_starters_if_empty`` so the system Plan is available
    immediately after the first API startup. Also safe to call manually on
    an existing DB — the plan_id primary-key check prevents double-insertion.

    ``repo`` defaults to ``Settings.system_plan_repo`` (read from
    ``TREADMILL_SYSTEM_PLAN_REPO`` env var) when not provided explicitly.

    Returns ``True`` if the Plan was created, ``False`` if already present.
    """
    from treadmill_api.models.event import Event
    from treadmill_api.models.plan import Plan

    if repo is None:
        from treadmill_api.config import get_settings
        repo = get_settings().system_plan_repo

    existing = session.get(Plan, SYSTEM_PLAN_ID)
    if existing is not None:
        logger.debug(
            "seed_system_plan_if_absent: system Plan %s already present; skipping",
            SYSTEM_PLAN_ID,
        )
        return False

    session.add(Plan(
        id=SYSTEM_PLAN_ID,
        repo=repo,
        intent=SYSTEM_PLAN_TITLE,
        created_by="scheduler",
    ))
    session.flush()

    # Insert a plan.activated event so dispatch_task's plan-active gate passes
    # immediately. The system Plan is always "active" — it never goes through
    # the wf-plan drafting/planning lifecycle.
    session.add(Event(
        entity_type="plan",
        action="activated",
        plan_id=SYSTEM_PLAN_ID,
        payload={"repo": repo, "title": SYSTEM_PLAN_TITLE},
    ))
    session.commit()
    logger.info(
        "seed_system_plan_if_absent: created system Plan %s (repo=%s)",
        SYSTEM_PLAN_ID, repo,
    )
    return True


# ── HTTP-driven check path ────────────────────────────────────────────────────


class _SeedClient(Protocol):
    """The subset of ``treadmill_cli.api_client.ApiClient`` needed here."""

    def _request(self, method: str, path: str, **kwargs: Any) -> Any: ...


def seed_system_plan(
    api_client: _SeedClient,
    *,
    repo: str | None = None,
) -> bool:
    """Check whether the system Plan is present via the HTTP API.

    Returns ``True`` if the system Plan exists (HTTP 200), ``False`` if not.
    Note: creation via HTTP is not supported (the plans API does not accept
    a custom ``id``). To create the system Plan, restart the API (auto-seed
    via ``seed_starters_if_empty``) or use ``seed_system_plan_if_absent``
    with a direct DB session.
    """
    if repo is None:
        from treadmill_api.config import get_settings
        repo = get_settings().system_plan_repo

    try:
        api_client._request("GET", f"/api/v1/plans/{SYSTEM_PLAN_ID}")
        logger.info(
            "seed_system_plan: system Plan %s is present (repo=%s)",
            SYSTEM_PLAN_ID, repo,
        )
        return True
    except Exception as exc:
        from treadmill_cli.api_client import ApiError  # local import; only on error
        if isinstance(exc, ApiError) and exc.status_code == 404:
            logger.warning(
                "seed_system_plan: system Plan %s not found — "
                "restart the API to auto-seed it via seed_starters_if_empty",
                SYSTEM_PLAN_ID,
            )
            return False
        raise
