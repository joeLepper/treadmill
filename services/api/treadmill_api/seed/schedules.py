"""Seed the four first-consumer ops-bot periodic schedules.

Parallel to ``starters.py`` for the workflow/role layer, this module seeds
the canonical periodic schedules that drive the ops automation layer on
first deploy (ADR-0035, plan task ``seed-schedules``).

Two paths:

  * ``seed_schedules(api_client)`` — HTTP-driven; idempotent via GET-first
    check (workflow_id + cron_expression key). Returns newly-created count.

  * ``seed_schedules_if_empty(session)`` — direct DB INSERT at API startup.
    Skips when any schedule rows already exist. Returns count seeded.

Both paths are idempotent: re-running seed is safe.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("treadmill.api.seed.schedules")


# ── Canonical seed schedules ─────────────────────────────────────────────────

SEED_SCHEDULES: list[dict[str, Any]] = [
    {
        # Weekly doc-drift audit — ADR-0032 Q32.f. Quiet hours match the
        # off-hours window so the audit runs during the work week, not
        # in the middle of the night when no one is watching.
        "workflow_id": "wf-documentarian-audit",
        "cron_expression": "0 9 * * 1",   # Monday 9am Pacific
        "quiet_hours": "20-6",
        "quiet_tz": "America/Los_Angeles",
        "payload_template": {"trigger": "scheduled-audit"},
        "jitter_seconds": 60,
        "created_by": "auto-seed",
    },
    {
        # Weekly learnings-crystallization sweep — ADR-0034 Q34.d.
        "workflow_id": "wf-crystallize-learning",
        "cron_expression": "0 20 * * 0",  # Sunday 8pm Pacific
        "quiet_hours": None,
        "quiet_tz": "America/Los_Angeles",
        "payload_template": {"trigger": "scheduled-sweep"},
        "jitter_seconds": 60,
        "created_by": "auto-seed",
    },
    {
        # Stuck-task sweep — empirically validated 2026-05-14 by the
        # parser silent-stall (task 0ac62421). Detects tasks whose last
        # event is older than the configurable threshold and whose last
        # step.completed had decision=fail without a downstream dispatch.
        # Frequent cadence: the cost of a tick is small; silence is hours.
        "workflow_id": "wf-stuck-task-sweep",
        "cron_expression": "*/10 * * * *",  # every 10 minutes
        "quiet_hours": None,
        "quiet_tz": "America/Los_Angeles",
        "payload_template": {"trigger": "scheduled-sweep"},
        "jitter_seconds": 60,
        "created_by": "auto-seed",
    },
    {
        # Observability regression scan — consumes the ADR-0020 stack.
        # Short-circuits to a no-op until Grafana/Loki/Tempo queries
        # succeed (ADR-0020 phase 3+).
        "workflow_id": "wf-o11y-regression-scan",
        "cron_expression": "*/15 * * * *",  # every 15 minutes
        "quiet_hours": None,
        "quiet_tz": "America/Los_Angeles",
        "payload_template": {"trigger": "scheduled-scan"},
        "jitter_seconds": 60,
        "created_by": "auto-seed",
    },
    {
        # ADR-0053 Wave 3: weekly judge-prompt optimizer run, targeting
        # role-architect first (the highest-leverage judge — its
        # amend/supersede/accept verdicts shape loop count). The
        # ``payload_template`` MUST carry ``repo`` per the schedule-payload-
        # needs-repo finding: the taskless dispatch path uses
        # ``rendered_payload["repo"]`` for the worker workspace, and an
        # empty/missing repo causes the dispatched ``step.ready`` to carry
        # ``repo=""`` → worker can't clone → step hangs pending forever.
        # The worker also pulls the labeled corpus via
        # ``$TREADMILL_CORPUS_S3_URI`` (set when the operator configures
        # ``aws.corpus_s3_uri`` in the deployment YAML; see
        # ``LocalRuntime._dev_local_worker_env``).
        "workflow_id": "wf-tune-judge-prompts",
        "cron_expression": "0 20 * * 6",  # Saturday 8pm Pacific
        "quiet_hours": None,
        "quiet_tz": "America/Los_Angeles",
        "payload_template": {
            "trigger": "scheduled-tune",
            "repo": "joeLepper/treadmill",
            "judge_role": "role-architect",
        },
        "jitter_seconds": 60,
        "created_by": "auto-seed",
    },
]


# ── Error type ────────────────────────────────────────────────────────────────


class ScheduleSeedError(Exception):
    """Raised when schedule seeding fails for a reason other than idempotency.

    Already-seeded schedules are silently skipped. Anything else (API error,
    network failure) surfaces so the operator can investigate.
    """


# ── HTTP-driven seed path ─────────────────────────────────────────────────────


class _SeedClient(Protocol):
    """The subset of ``treadmill_cli.api_client.ApiClient`` needed here."""

    def _request(self, method: str, path: str, **kwargs: Any) -> Any: ...


def seed_schedules(api_client: _SeedClient) -> int:
    """Seed the canonical schedules via the API CRUD endpoints.

    Fetches existing schedules first; only POSTs those not already present
    (matched by workflow_id + cron_expression). Idempotent: re-runs are
    no-ops for already-seeded schedules.

    Returns the count of newly created schedules.
    """
    try:
        existing = api_client._request("GET", "/api/v1/schedules")
    except Exception as exc:
        from treadmill_cli.api_client import ApiError  # local import; only on error
        if isinstance(exc, ApiError):
            raise ScheduleSeedError(
                f"listing existing schedules failed: {exc.detail}"
            ) from exc
        raise

    existing_keys = {
        (s["workflow_id"], s["cron_expression"]) for s in existing
    }

    created = 0
    for sched in SEED_SCHEDULES:
        key = (sched["workflow_id"], sched["cron_expression"])
        if key in existing_keys:
            logger.debug(
                "seed_schedules: %r already present; skipping",
                sched["workflow_id"],
            )
            continue
        try:
            api_client._request(
                "POST",
                "/api/v1/schedules",
                json={
                    "cron_expression": sched["cron_expression"],
                    "workflow_id": sched["workflow_id"],
                    "jitter_seconds": sched["jitter_seconds"],
                    "quiet_hours": sched["quiet_hours"],
                    "quiet_tz": sched["quiet_tz"],
                    "payload_template": sched["payload_template"],
                    "created_by": sched["created_by"],
                },
            )
            created += 1
            logger.info("seed_schedules: created schedule %r", sched["workflow_id"])
        except Exception as exc:
            from treadmill_cli.api_client import ApiError  # local import; only on error
            if isinstance(exc, ApiError):
                raise ScheduleSeedError(
                    f"seeding schedule {sched['workflow_id']!r} failed: {exc.detail}"
                ) from exc
            raise

    return created


# ── Startup-time direct-DB seed path ─────────────────────────────────────────


def seed_schedules_if_empty(session: Any) -> int:
    """Bulk-INSERT the canonical schedules into a fresh DB.

    Called from the API startup path after ``alembic upgrade head`` succeeds.
    Idempotent: when any schedule row exists, returns 0 without inserting.

    Returns the count of newly seeded schedules (0 on re-run; 4 on a fresh DB).
    """
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from treadmill_api.models.schedule import Schedule

    count = session.execute(
        sa_select(func.count(Schedule.id))
    ).scalar_one()
    if count > 0:
        logger.debug(
            "seed_schedules_if_empty: %d schedules already present; skipping",
            count,
        )
        return 0

    for sched in SEED_SCHEDULES:
        session.add(Schedule(
            cron_expression=sched["cron_expression"],
            workflow_id=sched["workflow_id"],
            jitter_seconds=sched["jitter_seconds"],
            quiet_hours=sched["quiet_hours"],
            quiet_tz=sched["quiet_tz"],
            payload_template=sched["payload_template"],
            created_by=sched["created_by"],
            status="active",
        ))

    session.commit()
    logger.info(
        "seed_schedules_if_empty: seeded %d schedules", len(SEED_SCHEDULES)
    )
    return len(SEED_SCHEDULES)
