"""Structural tests for the trace-replay fixture expansion (task 4e3cffc2).

The expansion adds two new step-transition paths to the synthetic
fixture without requiring the integration baseline regen:

  * **wf-feedback-loop**: a wf-feedback (analyzer→action) run dispatched
    by the existing ti=2 changes_requested decision. The action step
    completes with ``validation_fail`` so the trace exercises
    ``_maybe_fire_feedback_validation_fail_arbitration`` — a router
    helper the 56-event baseline did not reach.
  * **conflict-resolution**: a wf-conflict (analyzer→action) run
    dispatched by a github.pr_dirty event against task #0's PR. The
    action step completes with ``pass`` (clean rebase outcome).

These tests verify the fixture's structural invariants directly —
event shapes, decision values, seed-manifest entries — so coverage
of the new paths is asserted in CI without depending on the
integration test's baseline regen (which requires Docker + Postgres,
gated behind ``TREADMILL_INTEGRATION=1``).

The integration test ``test_trace_replay_matches_baseline`` continues
to assert end-to-end equivalence against a frozen sidecar; this file
asserts the fixture's CONTENT independently so a regression in the
generator surfaces immediately on every CI run.
"""
from __future__ import annotations

import gzip
import json
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pytest


_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_EVENTS_PATH = _FIXTURES_DIR / "coordination_trace_synthetic_events.jsonl.gz"
_SEED_PATH = _FIXTURES_DIR / "coordination_trace_synthetic_seed.json"

# Mirror of the generator's _u() so the test can compute the same
# deterministic task ids without depending on the generator script.
_NS = uuid.UUID("00000000-0000-0000-0000-000000000099")


def _u(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


_FEEDBACK_TASK_ID = _u("feedback-task-0")
_CONFLICT_TASK_ID = _u("conflict-task-0")


@pytest.fixture(scope="module")
def events() -> list[dict[str, Any]]:
    with gzip.open(_EVENTS_PATH, "rt") as f:
        return [json.loads(line) for line in f]


@pytest.fixture(scope="module")
def seed() -> dict[str, Any]:
    with _SEED_PATH.open() as f:
        return json.load(f)


# ── Top-line invariants ──────────────────────────────────────────────────


def test_fixture_event_count_expanded(events: list[dict[str, Any]]) -> None:
    """Total event count grew past the original 56 (PR #263 baseline).

    The expansion is two new step-transition flows + one new github
    action, so the floor is 56 + ~14. Hard-cap at 200 catches a
    runaway generator (e.g. accidentally adding a loop over N_TASKS).
    """
    assert 56 < len(events) <= 200, (
        f"event count {len(events)} outside expected expansion bounds "
        f"(was 56 pre-task-4e3cffc2)"
    )


def test_fixture_includes_pr_dirty(events: list[dict[str, Any]]) -> None:
    """conflict-resolution path: github.pr_dirty event must be present
    in the trace. This is the dispatch signal that triggers wf-conflict."""
    pr_dirty = [e for e in events if e["entity_type"] == "github" and e["action"] == "pr_dirty"]
    assert len(pr_dirty) == 1, (
        f"expected exactly 1 github.pr_dirty event for conflict-resolution "
        f"coverage; got {len(pr_dirty)}"
    )
    payload = pr_dirty[0]["payload"]
    assert payload.get("mergeable_state") == "dirty"
    assert payload.get("pr_number") == 1000, (
        "pr_dirty must target task-0's PR (pr_number=1000) so the "
        "fixture's existing per-task github events back-reference the "
        "conflict scenario"
    )


# ── wf-feedback-loop path ────────────────────────────────────────────────


def test_feedback_task_registered(events: list[dict[str, Any]]) -> None:
    """A task.registered event for the synthetic feedback task must be
    present — it's the entry point for wf-feedback dispatch."""
    feedback_regs = [
        e for e in events
        if e["entity_type"] == "task" and e["action"] == "registered"
        and "feedback" in (e["payload"].get("title") or "").lower()
    ]
    assert len(feedback_regs) == 1, (
        "expected exactly 1 task.registered for the wf-feedback synthetic run"
    )


def test_feedback_run_has_analyzer_and_action_steps(
    events: list[dict[str, Any]],
) -> None:
    """The wf-feedback run produces ready/started/completed for both
    its analyzer step and its action step — 6 step events total for
    the new run. Discriminate by task_id since only the ``ready`` event
    payload carries the step_name field."""
    feedback_step_events = [
        e for e in events
        if e["entity_type"] == "step"
        and e.get("task_id") == _FEEDBACK_TASK_ID
    ]
    # 2 steps × 3 events (ready/started/completed) = 6
    assert len(feedback_step_events) == 6, (
        f"wf-feedback run should have exactly 6 step events "
        f"(2 steps × ready/started/completed); got {len(feedback_step_events)}"
    )
    actions = Counter(e["action"] for e in feedback_step_events)
    assert actions == Counter({"ready": 2, "started": 2, "completed": 2})


def test_feedback_action_step_decision_drives_arbitration(
    events: list[dict[str, Any]],
) -> None:
    """The wf-feedback action step's terminal decision must be
    ``validation_fail`` — that's the value that drives
    ``_maybe_fire_feedback_validation_fail_arbitration``, the routing
    helper the 56-event baseline did not exercise."""
    completed = [
        e for e in events
        if e["entity_type"] == "step" and e["action"] == "completed"
    ]
    feedback_action_completed = [
        e for e in completed
        if e["payload"].get("output", {}).get("summary", "")
            .startswith("Feedback step-feedback-action")
    ]
    assert len(feedback_action_completed) == 1
    decision = (
        feedback_action_completed[0]["payload"]["output"]["decision"]
    )
    assert decision == "validation_fail", (
        f"feedback action step decision must be 'validation_fail' to "
        f"drive arbitration; got {decision!r}"
    )


# ── conflict-resolution path ─────────────────────────────────────────────


def test_conflict_task_registered_after_pr_dirty(
    events: list[dict[str, Any]],
) -> None:
    """A task.registered event for the synthetic conflict task must
    follow the github.pr_dirty event chronologically — the
    pr_dirty→register sequence is the conflict-dispatch lifecycle."""
    pr_dirty = next(
        (e for e in events if e["entity_type"] == "github" and e["action"] == "pr_dirty"),
        None,
    )
    assert pr_dirty is not None
    conflict_regs = [
        e for e in events
        if e["entity_type"] == "task" and e["action"] == "registered"
        and "conflict" in (e["payload"].get("title") or "").lower()
    ]
    assert len(conflict_regs) == 1, (
        "expected exactly 1 task.registered for the wf-conflict synthetic run"
    )
    # Chronological order: pr_dirty's minute < conflict task's minute.
    assert pr_dirty["created_at"] < conflict_regs[0]["created_at"]


def test_conflict_run_has_analyzer_and_action_steps(
    events: list[dict[str, Any]],
) -> None:
    """The wf-conflict run produces ready/started/completed for both
    its analyzer step and its action step. Discriminate by task_id
    (same shape as the feedback test above)."""
    conflict_step_events = [
        e for e in events
        if e["entity_type"] == "step"
        and e.get("task_id") == _CONFLICT_TASK_ID
    ]
    assert len(conflict_step_events) == 6, (
        f"wf-conflict run should have exactly 6 step events; "
        f"got {len(conflict_step_events)}"
    )
    actions = Counter(e["action"] for e in conflict_step_events)
    assert actions == Counter({"ready": 2, "started": 2, "completed": 2})


def test_conflict_action_step_decision_pass(events: list[dict[str, Any]]) -> None:
    """The wf-conflict action step completes with ``pass`` (clean
    rebase). The cap-reached / no-progress branches are explicitly
    out of scope for this expansion."""
    completed = [
        e for e in events
        if e["entity_type"] == "step" and e["action"] == "completed"
    ]
    conflict_action_completed = [
        e for e in completed
        if e["payload"].get("output", {}).get("summary", "")
            .startswith("Conflict step-conflict-action")
    ]
    assert len(conflict_action_completed) == 1
    decision = (
        conflict_action_completed[0]["payload"]["output"]["decision"]
    )
    assert decision == "pass"


# ── Seed manifest ────────────────────────────────────────────────────────


def test_seed_includes_new_workflows(seed: dict[str, Any]) -> None:
    """The seed must declare wf-feedback-synthetic + wf-conflict-synthetic
    so the new runs' rows have valid foreign keys in a clean test DB."""
    workflow_ids = {w["id"] for w in seed["workflows"]}
    assert "wf-feedback-synthetic" in workflow_ids
    assert "wf-conflict-synthetic" in workflow_ids


def test_seed_includes_workflow_versions(seed: dict[str, Any]) -> None:
    """Each new workflow has a v1 row in workflow_versions."""
    versions_by_workflow = {wv["workflow_id"] for wv in seed["workflow_versions"]}
    assert "wf-feedback-synthetic" in versions_by_workflow
    assert "wf-conflict-synthetic" in versions_by_workflow


def test_seed_step_count_matches_expansion(seed: dict[str, Any]) -> None:
    """3 (wf-author) + 2 (wf-feedback) + 2 (wf-conflict) = 7 step
    definitions total in workflow_version_steps."""
    assert len(seed["workflow_version_steps"]) == 7


def test_seed_run_count_matches_expansion(seed: dict[str, Any]) -> None:
    """5 (per-task author runs) + 1 (feedback) + 1 (conflict) = 7
    workflow_runs total."""
    assert len(seed["workflow_runs"]) == 7


def test_seed_role_set_includes_new_roles(seed: dict[str, Any]) -> None:
    """The role union must include the new analyzer roles; the action
    step's ``role-code-author`` is shared with the existing author
    workflow so total role count increases by 2, not 4."""
    role_ids = {r["id"] for r in seed["roles"]}
    assert "role-feedback-analyzer" in role_ids
    assert "role-conflict-analyzer" in role_ids
    # role-code-author is reused; original ROLES has 3 + 2 new = 5 unique.
    assert "role-code-author" in role_ids


# ── Distribution sanity ─────────────────────────────────────────────────


def test_no_event_type_count_regressed(events: list[dict[str, Any]]) -> None:
    """Every entity/action pair the 56-event baseline emitted is still
    present at >= its original count. Tripwire against an accidental
    delete during the expansion edit."""
    counts = Counter((e["entity_type"], e["action"]) for e in events)
    baseline_floor = {
        ("plan", "activated"): 1,
        ("schedule", "tick"): 1,
        ("task", "registered"): 5,
        ("task", "cancelled"): 1,
        ("task", "architect_emit_failure"): 1,
        ("step", "ready"): 14,
        ("step", "started"): 14,
        ("step", "completed"): 13,
        ("step", "failed"): 1,
        ("github", "pr_opened"): 3,
        ("github", "pr_merged"): 2,
    }
    for key, floor in baseline_floor.items():
        assert counts.get(key, 0) >= floor, (
            f"{key[0]}.{key[1]} count regressed: got {counts.get(key, 0)}, "
            f"baseline floor {floor}"
        )
