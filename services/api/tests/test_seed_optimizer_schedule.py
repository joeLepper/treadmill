"""Seeded ``wf-tune-judge-prompts`` schedule (ADR-0053 Wave 3).

Structural assertions over ``SEED_SCHEDULES``: the optimizer schedule
must exist with the right cron, and its ``payload_template`` must carry
``repo`` (per the schedule-payload-needs-repo finding — without ``repo``
the dispatched ``step.ready`` event carries ``repo=""`` and the worker
hangs on clone) + ``judge_role`` (so the optimizer knows which judge to
tune).
"""

from __future__ import annotations

from treadmill_api.seed.schedules import SEED_SCHEDULES


def _find_schedule(workflow_id: str, cron: str = "0 20 * * 6") -> dict:
    """Find the single row for (workflow_id, cron). Strategy A (ADR-0056)
    seeds ``wf-tune-judge-prompts`` twice under different crons, so the
    legacy workflow-id-only lookup is ambiguous; this helper defaults to
    the Saturday role-architect row so the existing optimizer-schedule
    tests keep targeting it."""
    matches = [
        s for s in SEED_SCHEDULES
        if s["workflow_id"] == workflow_id and s["cron_expression"] == cron
    ]
    assert len(matches) == 1, (
        f"expected exactly one SEED_SCHEDULES entry for "
        f"{workflow_id!r} cron={cron!r}, got {len(matches)}"
    )
    return matches[0]


def test_optimizer_schedule_present() -> None:
    """The Wave 3 schedule for ``wf-tune-judge-prompts`` exists."""
    sched = _find_schedule("wf-tune-judge-prompts")
    assert sched["cron_expression"] == "0 20 * * 6"  # Saturday 8pm Pacific
    assert sched["status"] == "active" if "status" in sched else True
    assert sched["created_by"] == "auto-seed"


def test_optimizer_payload_carries_repo() -> None:
    """``payload_template`` MUST include ``repo`` — without it, the
    dispatched ``step.ready`` event carries ``repo=""`` and the worker
    can't clone a workspace (see project_schedule_payload_needs_repo)."""
    sched = _find_schedule("wf-tune-judge-prompts")
    payload = sched["payload_template"]
    assert "repo" in payload, (
        f"payload_template missing 'repo' — taskless dispatch will hang. "
        f"Got: {payload}"
    )
    assert payload["repo"], "payload_template['repo'] must be non-empty"


def test_optimizer_payload_specifies_judge_role() -> None:
    """``judge_role`` tells the optimizer which judge to tune."""
    sched = _find_schedule("wf-tune-judge-prompts")
    assert sched["payload_template"].get("judge_role") == "role-architect"


def test_existing_schedules_unchanged() -> None:
    """Regression net: every prior schedule slug must still be present.
    Strategy A (ADR-0056 canary) added a second ``wf-tune-judge-prompts``
    row (role-code-author Sunday) under the same slug; ADR-0047/0038/0042
    added ``wf-terminal-gate-sweep``, so the total row count is now 9."""
    workflow_ids = [s["workflow_id"] for s in SEED_SCHEDULES]
    for expected in (
        "wf-documentarian-audit",
        "wf-crystallize-learning",
        "wf-stuck-task-sweep",
        "wf-o11y-regression-scan",
        "wf-tune-judge-prompts",
        "wf-ui-triage",  # ADR-0061
        "wf-escalation-close-sweep",  # ADR-0062 Step 2
        "wf-terminal-gate-sweep",  # ADR-0047/0038/0042
    ):
        assert expected in workflow_ids, f"missing {expected} in SEED_SCHEDULES"
    assert len(SEED_SCHEDULES) == 9, (
        f"expected 9 schedules, got {len(SEED_SCHEDULES)}: {workflow_ids}"
    )
