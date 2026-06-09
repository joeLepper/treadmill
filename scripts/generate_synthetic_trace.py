"""Generate a synthetic event trace + seed manifest for the trace-replay harness.

The pre-existing RAMJAC fixture had a 21% defect rate (307/1453 lines
malformed) because the original capture pipeline serialized embedded
patch payloads without an outer ``json.dumps()`` pass — quotes inside
diff content escaped to the JSON string layer and broke parsing.

This generator avoids that class of defect by construction: every
event is built as a Python dict and serialized via ``json.dumps()``
with proper escaping, then the output JSONL is re-parsed line-by-line
as a self-test before the script exits.

Outputs (committed alongside the script's invocation):

* ``services/api/tests/fixtures/coordination_trace_synthetic_events.jsonl.gz``
  — the gzipped JSONL trace, ~100 events covering every entity-type
  branch in ``CoordinationConsumer.handle()`` (plan, github, schedule,
  task, step) and several payloads with embedded quote-bearing content
  (the failure shape the old fixture was bitten by).
* ``services/api/tests/fixtures/coordination_trace_synthetic_seed.json``
  — JSON manifest of rows to insert into a clean test DB BEFORE replay
  so the routing helpers don't short-circuit on missing-row lookups.
  Includes plans, workflows, workflow_versions, workflow_version_steps,
  roles, tasks, and the workflow_runs + workflow_run_steps rows the
  step events project into.

Usage::

    python scripts/generate_synthetic_trace.py

Idempotent (same UUIDs / counts on every run by construction — the
script uses fixed deterministic IDs so commits round-trip cleanly).

Re-generate when the event schema evolves (e.g. a new entity-type
branch lands in handle()) so the harness keeps covering every routing
path. Regenerate-then-recapture is the canonical refresh workflow;
see ``scripts/capture_trace_baseline.py``.
"""

from __future__ import annotations

import gzip
import json
import sys
import uuid
from pathlib import Path
from typing import Any

# Deterministic UUIDs. Re-running the generator produces byte-identical
# output (modulo line-ending nondeterminism), which keeps commits clean
# and the test sidecar stable across regenerations.
_NS = uuid.UUID("00000000-0000-0000-0000-000000000099")


def _u(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


PLAN_ID = _u("plan-synthetic-001")
WORKFLOW_ID = "wf-author-synthetic"
WORKFLOW_VERSION_ID = _u("wfv-author-synthetic-v1")
STEP_NAMES = ("step-a-author", "step-b-validate", "step-c-review")
ROLES = ("role-author", "role-validate", "role-review")

# Per-step UUIDs at the WorkflowVersionStep layer (different from the
# per-run WorkflowRunStep UUIDs the events project into).
WFV_STEP_IDS = {name: _u(f"wfv-step-{name}") for name in STEP_NAMES}

REPO = "acme/widget"  # domain-neutral, mirrors the scrub convention

# 5 tasks. Each runs the same 3-step workflow with slight variation in
# how the run terminates (clean completion, decision=fail, step.failed,
# changes_requested, gate-broken).
N_TASKS = 5

# Output paths (relative to the repo root the script is invoked from).
FIXTURES_DIR = Path("services/api/tests/fixtures")
EVENTS_PATH = FIXTURES_DIR / "coordination_trace_synthetic_events.jsonl.gz"
SEED_PATH = FIXTURES_DIR / "coordination_trace_synthetic_seed.json"


# ── helpers ────────────────────────────────────────────────────────────────────


def _ts(minutes: int) -> str:
    """Stable ISO-8601 timestamp at synthetic-epoch + N minutes."""
    base_minute = 30 + minutes
    hh, mm = divmod(base_minute, 60)
    return f"2026-06-01T{15 + hh:02d}:{mm:02d}:00+00:00"


def _event(
    *,
    entity_type: str,
    action: str,
    payload: dict[str, Any],
    plan_id: str | None = PLAN_ID,
    task_id: str | None = None,
    run_id: str | None = None,
    step_id: str | None = None,
    event_id: str | None = None,
    minute: int = 0,
) -> dict[str, Any]:
    """Build one event record matching the SQS-message envelope shape
    ``CoordinationConsumer.handle()`` reads.

    Note ``event_id`` lives at the top level (not ``id``) — that's the
    key ``persist_audit_row`` reads; the old captured fixture used ``id``
    and silently no-op'd the audit INSERT.
    """
    return {
        "event_id": event_id or _u(f"evt-{entity_type}-{action}-{minute}-{task_id}"),
        "entity_type": entity_type,
        "action": action,
        "plan_id": plan_id,
        "task_id": task_id,
        "run_id": run_id,
        "step_id": step_id,
        "payload": payload,
        "created_at": _ts(minute),
    }


# ── trace + seed ──────────────────────────────────────────────────────────────


def build_trace() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Produce the (events, seed_manifest) pair."""
    events: list[dict[str, Any]] = []
    workflow_runs: list[dict[str, Any]] = []
    workflow_run_steps: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []

    # 0. plan.activated kicks off the lifecycle.
    events.append(_event(
        entity_type="plan",
        action="activated",
        payload={"plan_id": PLAN_ID, "activated_by": "operator-trigger"},
        minute=0,
    ))

    # 1. schedule.tick — non-step entity branch coverage.
    events.append(_event(
        entity_type="schedule",
        action="tick",
        payload={"schedule_id": _u("sched-001"), "workflow_id": WORKFLOW_ID},
        plan_id=None,
        minute=1,
    ))

    # 2-N. per-task lifecycle.
    for ti in range(N_TASKS):
        task_id = _u(f"task-{ti}")
        run_id = _u(f"run-{ti}")
        minute_base = 2 + ti * 10

        tasks.append({
            "id": task_id,
            "plan_id": PLAN_ID,
            "repo": REPO,
            "title": f"Synthetic task {ti}",
            "description": "Designed to exercise routing helpers in trace-replay.",
            "workflow_version_id": WORKFLOW_VERSION_ID,
            "created_by": "synthetic-generator",
        })
        workflow_runs.append({
            "id": run_id,
            "task_id": task_id,
            "workflow_version_id": WORKFLOW_VERSION_ID,
            "trigger": "registered",
        })

        # task.registered fires first.
        events.append(_event(
            entity_type="task",
            action="registered",
            payload={
                "repo": REPO,
                "title": tasks[-1]["title"],
                "plan_id": PLAN_ID,
                "workflow_version_id": WORKFLOW_VERSION_ID,
            },
            task_id=task_id,
            minute=minute_base,
        ))

        # Each of the three steps: pending → ready → started → completed.
        for si, step_name in enumerate(STEP_NAMES):
            step_id = _u(f"run-{ti}-step-{step_name}")
            workflow_run_steps.append({
                "id": step_id,
                "run_id": run_id,
                "step_index": si,
                "step_name": step_name,
                "role_id": ROLES[si],
                "status": "pending",
            })

            step_minute = minute_base + 1 + si * 2

            events.append(_event(
                entity_type="step",
                action="ready",
                payload={
                    "step_id": step_id,
                    "task_id": task_id,
                    "run_id": run_id,
                    "step_name": step_name,
                    "role_id": ROLES[si],
                    "dispatched_at": _ts(step_minute),
                },
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                minute=step_minute,
            ))
            events.append(_event(
                entity_type="step",
                action="started",
                payload={"started_at": _ts(step_minute)},
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                minute=step_minute,
            ))

            # Vary the terminal action per (task_index, step_index) so
            # the trace covers fail/failed/architect-emit + cancelled.
            is_last_step = si == len(STEP_NAMES) - 1
            output_decision = "pass"
            if ti == 1 and si == 1:
                output_decision = "fail"  # exercises validate-feedback
            if ti == 2 and si == 2:
                output_decision = "changes_requested"  # exercises review-feedback
            if ti == 3 and is_last_step:
                output_decision = "gate-broken"  # exercises gate-broken escalation

            # Branch artifact gates write_task_prs (paired with the
            # ``payload.pr_number``); only present on first-step output
            # so only the author step triggers the D.8 drain.
            artifacts = (
                [{"kind": "branch", "value": f"task/{task_id[:8]}-synthetic-{ti}"}]
                if si == 0
                else []
            )
            output_payload = {
                "summary": f"Step {step_name} terminal for task {ti}",
                "decision": output_decision,
                "commit_sha": "deadbeefcafe",
                "artifacts": artifacts,
                # Embed a quote-bearing patch fragment so the JSON encoder
                # has something to actually escape — this is the failure
                # shape the old fixture was bitten by. The pr_number lives
                # here (inside ``output.payload``) per the projector's
                # write_task_prs contract — top-level ``payload.pr_number``
                # is NOT read.
                "payload": {
                    "patch_fragment": (
                        "+ORGANIZATION_ID=\"YOUR_ORG_ID\"\n"
                        "+BILLING_ACCOUNT_ID=\"YOUR_BILLING_ID\"\n"
                    ),
                    **(
                        {"pr_number": 1000 + ti}
                        if si == 0
                        else {}
                    ),
                },
                "metadata": {},
            }
            # Step.failed lifecycle on task #4 step #1 — sibling helper coverage.
            if ti == 4 and si == 1:
                events.append(_event(
                    entity_type="step",
                    action="failed",
                    payload={
                        "failed_at": _ts(step_minute),
                        "error": "synthetic worker crash for trace coverage",
                    },
                    task_id=task_id,
                    run_id=run_id,
                    step_id=step_id,
                    minute=step_minute,
                ))
                # Don't dispatch later steps — failed terminates the run.
                break

            events.append(_event(
                entity_type="step",
                action="completed",
                payload={
                    "completed_at": _ts(step_minute),
                    "output": output_payload,
                },
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                minute=step_minute,
            ))

        # Per-task github events. Schema-conformant payloads (sender,
        # title, head_sha required on pr_opened; sender required on
        # pr_merged) — without these the github branch's pydantic
        # validator drops the event before evaluate_triggers can fire.
        github_action = "pr_opened" if ti % 2 == 0 else "pr_merged"
        github_payload: dict[str, Any] = {
            "repo": REPO,
            "pr_number": 1000 + ti,
            "sender": "synthetic-bot",
        }
        if github_action == "pr_opened":
            github_payload.update({
                "title": f"Synthetic PR for task {ti}",
                "head_branch": f"task/{task_id[:8]}-synthetic-{ti}",
                "head_sha": "deadbeefcafe",
            })
        else:
            github_payload.update({
                "head_branch": f"task/{task_id[:8]}-synthetic-{ti}",
                "merged_sha": "deadbeefcafe",
            })
        events.append(_event(
            entity_type="github",
            action=github_action,
            payload=github_payload,
            plan_id=None,
            task_id=None,
            minute=minute_base + 9,
        ))

        # Cancelled task — exercises task-terminal-for-triage.
        if ti == 4:
            events.append(_event(
                entity_type="task",
                action="cancelled",
                payload={"reason": "synthetic cancellation for coverage"},
                task_id=task_id,
                minute=minute_base + 9,
            ))

    # 3. task.architect_emit_failure — ADR-0083 entity-type branch.
    events.append(_event(
        entity_type="task",
        action="architect_emit_failure",
        payload={
            "task_id": _u("task-0"),
            "reason": "synthetic emit failure for coverage",
        },
        task_id=_u("task-0"),
        minute=120,
    ))

    seed_manifest = {
        "plans": [{
            "id": PLAN_ID,
            "title": "Synthetic plan",
            "description": "Generated for trace-replay coverage.",
            "created_by": "synthetic-generator",
            "repo": REPO,
        }],
        "workflows": [{"id": WORKFLOW_ID, "description": "Synthetic workflow"}],
        "workflow_versions": [{
            "id": WORKFLOW_VERSION_ID,
            "workflow_id": WORKFLOW_ID,
            "version": 1,
        }],
        "roles": [{
            "id": role_id,
            "model": "claude-sonnet-4-6",
            "system_prompt": "Synthetic role for trace-replay seeding.",
            "output_kind": "ANALYSIS",
        } for role_id in ROLES],
        "workflow_version_steps": [{
            "id": WFV_STEP_IDS[name],
            "workflow_version_id": WORKFLOW_VERSION_ID,
            "step_index": i,
            "step_name": name,
            "role_id": ROLES[i],
        } for i, name in enumerate(STEP_NAMES)],
        # event_triggers — without these the github branch's
        # ``evaluate_triggers`` finds no candidate workflows + returns
        # without firing dispatcher.dispatch_task. Seeding the two
        # event_types my synthetic github events use exercises the
        # publish path and gives dispatcher_calls > 0 in the baseline.
        "event_triggers": [
            {
                "id": _u(f"trigger-{event_type}"),
                "repo": REPO,
                "event_type": event_type,
                "workflow_id": WORKFLOW_ID,
                "version_strategy": "latest",
                "enabled": True,
            }
            for event_type in ("pr_opened", "pr_merged")
        ],
        "tasks": tasks,
        "workflow_runs": workflow_runs,
        "workflow_run_steps": workflow_run_steps,
    }

    return events, seed_manifest


# ── post-output validation ────────────────────────────────────────────────────


def _validate_jsonl(path: Path) -> int:
    """Re-read the gzipped JSONL line-by-line and parse each. Aborts the
    script if any line fails — the whole point of the regeneration is
    that the output is parseable by construction."""
    count = 0
    with gzip.open(path, "rt") as f:
        for line_no, line in enumerate(f, start=1):
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                sys.stderr.write(
                    f"FATAL: line {line_no} in {path} failed to parse: "
                    f"{exc}\n  excerpt: {line[:200]!r}\n"
                )
                sys.exit(2)
            count += 1
    return count


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    events, seed = build_trace()

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # Serialize each event with proper escaping and stable key ordering.
    with gzip.open(EVENTS_PATH, "wt") as f:
        for record in events:
            f.write(json.dumps(record, sort_keys=True))
            f.write("\n")

    # Validate the output round-trip parses cleanly.
    count = _validate_jsonl(EVENTS_PATH)
    if count != len(events):
        sys.stderr.write(
            f"FATAL: emitted {len(events)} records, validation read "
            f"{count}\n"
        )
        return 3
    print(f"wrote {count} events to {EVENTS_PATH}; all lines parsed.")

    with SEED_PATH.open("w") as f:
        json.dump(seed, f, indent=2, sort_keys=True)
    print(
        f"wrote seed manifest to {SEED_PATH}: "
        f"{len(seed['plans'])} plans, "
        f"{len(seed['tasks'])} tasks, "
        f"{len(seed['workflow_runs'])} runs, "
        f"{len(seed['workflow_run_steps'])} steps."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
