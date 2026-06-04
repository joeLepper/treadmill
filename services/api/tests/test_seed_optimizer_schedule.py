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


def _find_schedule(workflow_id: str) -> dict:
    matches = [s for s in SEED_SCHEDULES if s["workflow_id"] == workflow_id]
    assert len(matches) == 1, (
        f"expected exactly one SEED_SCHEDULES entry for {workflow_id!r}, "
        f"got {len(matches)}"
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
    """Regression net: the Wave 3 seed addition (and Wave 4's three
    role-tuning rows on top) must not drop any prior schedule. The row
    count tracks rows, not unique workflow_ids: Wave 4 introduces a
    single new workflow_id (``wf-tune-role-prompts``) under three
    distinct crons (code-author / reviewer / validator)."""
    workflow_ids = [s["workflow_id"] for s in SEED_SCHEDULES]
    for expected in (
        "wf-documentarian-audit",
        "wf-crystallize-learning",
        "wf-stuck-task-sweep",
        "wf-o11y-regression-scan",
        "wf-tune-judge-prompts",
        "wf-tune-role-prompts",  # ADR-0056 Wave 4
        "wf-ui-triage",  # ADR-0061
        "wf-escalation-close-sweep",  # ADR-0062 Step 2
    ):
        assert expected in workflow_ids, f"missing {expected} in SEED_SCHEDULES"
    assert len(SEED_SCHEDULES) == 10, (
        f"expected 10 schedules (7 unique workflow_ids; Wave 4 fans "
        f"wf-tune-role-prompts across 3 crons), got "
        f"{len(SEED_SCHEDULES)}: {workflow_ids}"
    )
