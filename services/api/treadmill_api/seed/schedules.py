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
        # ADR-0062 Step 2: escalation-close sweep — paired with the
        # stuck-task sweep above. Iterates every open incident (latest
        # ``task.escalated_to_operator`` without a later
        # ``task.escalation_closed`` for the same task) and emits a
        # close event for each one whose underlying task has hit a
        # close trigger (re_progressed / pr_merged / cancelled /
        # superseded). Tight ``*/2`` cadence because incident latency
        # matters more than throughput — the sweep cost is a single
        # open-incidents query + a handful of close-trigger probes per
        # incident, and ADR-0062's Slack-channel-as-MTTR-log surface
        # wants the close visible within a couple of minutes.
        "workflow_id": "wf-escalation-close-sweep",
        "cron_expression": "*/2 * * * *",  # every 2 minutes
        "quiet_hours": None,
        "quiet_tz": "America/Los_Angeles",
        "payload_template": {"trigger": "scheduled-sweep"},
        "jitter_seconds": 60,
        "created_by": "auto-seed",
    },
    {
        # Terminal-gate orphan sweep — inverse of the stuck-task signal.
        # Detects tasks where the architect issued an accept-as-is verdict
        # (review.override / validate.override per ADR-0038 / ADR-0042) but
        # the PR was never merged. Cancelled and superseded tasks are
        # explicitly excluded — they legitimately leave a PR unmerged.
        # The escalation-close sweep (ADR-0062) auto-closes the incident
        # once github.pr_merged arrives. Frequent cadence mirrors the
        # stuck-task sweep: a tick is cheap (pure query) and prompt
        # operator visibility on accepted-but-unmerged PRs matters.
        "workflow_id": "wf-terminal-gate-sweep",
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
    {
        # ADR-0056 canary: weekly retrospective tuning for an AUTHOR role.
        # Same workflow slug as the role-architect row above (Strategy A —
        # ``wf-tune-judge-prompts`` is now role-agnostic; see the retitled
        # description in ``starters.py``). Sunday 21:00 Pacific avoids the
        # 20:00 learnings-crystallization tick. New rows use ``role_id``
        # (the canonical key per the role-prompt-optimizer prompt); the
        # role-architect row above keeps ``judge_role`` for backward
        # compat. ``repo`` is REQUIRED per the schedule-payload-needs-repo
        # finding — taskless dispatch reads ``rendered_payload["repo"]``
        # for the worker workspace, and an empty value hangs the step
        # pending forever.
        "workflow_id": "wf-tune-judge-prompts",
        "cron_expression": "0 21 * * 0",  # Sunday 9pm Pacific
        "quiet_hours": None,
        "quiet_tz": "America/Los_Angeles",
        "payload_template": {
            "repo": "joeLepper/treadmill",
            "role_id": "role-code-author",
        },
        "jitter_seconds": 60,
        "created_by": "auto-seed",
    },
    {
        # ADR-0061 Step 5: periodic UI triage via Playwright. Every 4 h on
        # minute 7 — offset from the hour to avoid the hourly thundering
        # herd while still landing several runs per day so the labelable
        # corpus accrues quickly. ``payload_template`` MUST carry ``repo``
        # for the same reason wf-tune-judge-prompts does — taskless
        # scheduled dispatch reads ``rendered_payload["repo"]`` for the
        # worker workspace, and an empty value hangs the step pending
        # forever (schedule-payload-needs-repo finding). ``target_urls`` is
        # the dashboard Overview only for v1; TaskDetail requires picking
        # a task at runtime, so periodic mode starts simpler — a future
        # ADR-0061 plan can expand surfaces. ``corpus_bucket`` is the S3
        # prefix where artifacts land; the role-side handles a missing
        # bucket best-effort (dev-local fully-local mode may not have it).
        "workflow_id": "wf-ui-triage",
        "cron_expression": "7 */4 * * *",  # minute 7, every 4 hours
        "quiet_hours": None,
        "quiet_tz": "America/Los_Angeles",
        "payload_template": {
            "repo": "joeLepper/treadmill",
            "trigger": "scheduled-tick",
            "mode": "periodic",
            "target_urls": ["http://localhost:5174/"],
            "corpus_bucket": "treadmill-personal-triage-corpus",
        },
        "jitter_seconds": 60,
        "created_by": "auto-seed",
    },
    {
        # ADR-0075 step-starvation detection — detects steps queued for
        # dispatch (step.ready) that never reach execution (step.started).
        # Tight ``* * * * *`` cadence (every 1 minute) because a stalled
        # queue blocks the task immediately; operator visibility and
        # recovery must be prompt. The sweep cost is a single
        # (task, step_index) pair query + a handful of escalations,
        # so frequent ticks are cost-effective.
        "workflow_id": "wf-step-starvation-sweep",
        "cron_expression": "* * * * *",  # every 1 minute
        "quiet_hours": None,
        "quiet_tz": "America/Los_Angeles",
        "payload_template": {"trigger": "scheduled-sweep"},
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

    Returns the count of newly seeded schedules (0 on re-run; len(SEED_SCHEDULES) on a fresh DB).
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
