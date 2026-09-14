"""Unit foils for the per-plan integration_base (ADR-0114), no DB required.

Covers the two seams the integration/round-trip tests can't isolate: the
``_to_plan_response`` mapping (Plan row → PlanResponse the coordinator reads) and the
``PlanHandoffPrOpened`` event validating WITHOUT a ``pr_number`` (the non-main-base
deliverable has no handoff PR)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from treadmill_api.events import parse_payload
from treadmill_api.models.plan import Plan
from treadmill_api.routers.plans import _to_plan_response


def _bare_plan(integration_base: str | None) -> Plan:
    p = Plan()
    p.id = uuid.uuid4()
    p.repo = "o/r"
    p.intent = None
    p.doc_path = "docs/plans/x.md"
    p.parent_plan_id = None
    p.created_by = "treadmill-donna"
    p.created_at = datetime.now(UTC)
    p.auto_merge = None
    p.integration_base = integration_base
    return p


def test_to_plan_response_exposes_integration_base() -> None:
    """The coordinator reads integration_base off GET /plans/{id}; the mapper must
    carry it through (non-null passes verbatim; null stays null → coordinator defaults
    to origin/main)."""
    base = "joes-agents/run-shape-telemetry-design"
    assert _to_plan_response(_bare_plan(base)).integration_base == base
    assert _to_plan_response(_bare_plan(None)).integration_base is None


def test_handoff_event_validates_without_pr_number() -> None:
    """ADR-0114: a non-main-base handoff has NO PR, so the event must validate with
    pr_number OMITTED (→ None) and still carry repo + branch + the branch URL. The
    default (main-base) handoff still carries a real pr_number."""
    branch = "joes-agents/2026-09-14-run-shape-investigation"
    no_pr = parse_payload(
        "plan",
        "handoff_pr_opened",
        {
            "repo": "netlify/agent-runner-orchestrator",
            "branch": branch,
            "pr_url": f"https://github.com/netlify/agent-runner-orchestrator/tree/{branch}",
        },
    )
    assert no_pr.pr_number is None
    assert no_pr.branch == branch

    with_pr = parse_payload(
        "plan",
        "handoff_pr_opened",
        {"repo": "o/r", "branch": branch, "pr_url": "https://x/pull/9", "pr_number": 9},
    )
    assert with_pr.pr_number == 9
