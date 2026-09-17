"""The approved-integration work-list — ADR-0119, the api→host-integrator contract.

ADR-0119 splits approve→integration: the api container DECIDES (records approvals to
``verdict_applications``) and a host-side integrator, running as the operator, EXECUTES the git
merge. This module is the SINGLE source of the candidate SELECTION both sides trust — exposed to
the host integrator over the API (``GET /api/v1/integration_queue``) so the host never
re-implements the query and cannot silently drift from the ADR-0118/#420 rules:

* LATEST approved head per task (``DISTINCT ON (task) ORDER BY created_at DESC``) — a re-approval
  integrates only the newest head, never a superseded one.
* NOT already integrated — no ``github.pr_merged`` for the task.
* NOT stuck AT THIS HEAD — no ``integration_conflict`` / ``integration_blocked`` /
  ``integration_stale_head`` escalation whose ``payload.head_sha`` equals this candidate's head.
  The exclusion is by ``(task, head)``, not by task (Bert #422): it stops re-selecting — and so
  re-escalating — a head the integrator already escalated (a ``stale_head`` recurs every poll
  otherwise, making the incident un-ackable), while still letting a FRESH approval at a NEW head
  through. A human clears a stuck head by re-evaluating; the new head is a new row, not excluded.
* feature-branch mode only (``team_configs.merge_target == 'feature-branch'``); ``main`` mode
  (``gh pr merge``) is a follow-on.

Slug/branch derivation (``integration_slug`` + ``is_valid_ref_component``, ADR-0118) is applied
here so the host receives a ready ``integration_branch`` — or ``slug_valid=False`` when the plan
doc basename is not a legal git ref, which the host escalates (``integration_blocked``) rather
than attempting an unpushable branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from treadmill_api.coordination.dispatch_consumer import (
    integration_slug,
    is_valid_ref_component,
)
from treadmill_api.coordination.integration_merger import integration_branch_for


@dataclass(frozen=True)
class IntegrationCandidate:
    """One approved task ready for the host integrator to merge. ``integration_branch`` is the
    derived ``joes-agents/<slug>`` when ``slug_valid``, else ``None`` (the host escalates)."""

    task_id: str
    repo: str
    pr_number: int | None
    head_sha: str
    integration_base: str
    slug_valid: bool
    integration_branch: str | None


# The candidate SELECTION — the single source of truth for "approved but not yet integrated"
# (ADR-0119). The host reads this via the API; keep the ADR-0118/#420 rules here and nowhere else.
#
# The head-keyed stuck-exclusion is applied POST-collapse (Bert #423): pick the LATEST approved
# head per task FIRST (the CTE's DISTINCT ON), THEN exclude the task iff THAT head is escalated.
# Applying it per-va-row instead would filter out an escalated LATEST head and resurface a
# SUPERSEDED earlier one — violating "integrate only the newest head." (A pre-#423 escalation
# event has no payload.head_sha, so `->>'head_sha'` is NULL and it stops excluding; harmless —
# the integrator never ran in prod before this.)
_CANDIDATES_SQL = text(
    "WITH latest AS ("
    "  SELECT DISTINCT ON (va.task_id) "
    "    va.task_id, t.repo, p.doc_path, COALESCE(p.integration_base, 'main') AS base, "
    "    va.head_sha, "
    "    (SELECT pr.pr_number FROM task_prs pr "
    "       WHERE pr.task_id = va.task_id AND pr.head_sha = va.head_sha "
    "       ORDER BY pr.created_at DESC LIMIT 1) AS pr_number "
    "  FROM verdict_applications va "
    "  JOIN tasks t ON t.id = va.task_id "
    "  JOIN plans p ON p.id = t.plan_id "
    "  JOIN team_configs tc ON tc.repo = t.repo "
    "  WHERE va.decision = 'approve' AND p.substrate = 'router' "
    "    AND tc.merge_target = 'feature-branch' "
    "    AND NOT EXISTS ("
    "      SELECT 1 FROM events e WHERE e.task_id = va.task_id AND e.action = 'pr_merged'"
    "    ) "
    "  ORDER BY va.task_id, va.created_at DESC"
    ") "
    "SELECT l.task_id, l.repo, l.doc_path, l.base, l.head_sha, l.pr_number "
    "FROM latest l "
    "WHERE NOT EXISTS ("
    "  SELECT 1 FROM events e2 WHERE e2.task_id = l.task_id "
    "    AND e2.action = 'escalated_to_operator' "
    "    AND e2.payload->>'reason' IN "
    "        ('integration_conflict','integration_blocked','integration_stale_head') "
    "    AND e2.payload->>'head_sha' = l.head_sha"
    ")"
)


async def approved_integration_candidates(session: Any) -> list[IntegrationCandidate]:
    """The current approved-not-integrated feature-branch work-list, latest head per task, with
    the derived integration branch. Pure read; no side effects (escalation is the host's job)."""
    rows = (await session.execute(_CANDIDATES_SQL)).all()
    candidates: list[IntegrationCandidate] = []
    for task_id, repo, doc_path, base, head_sha, pr_number in rows:
        slug = integration_slug(doc_path)
        valid = slug is not None and is_valid_ref_component(slug)
        candidates.append(
            IntegrationCandidate(
                task_id=str(task_id),
                repo=str(repo),
                pr_number=int(pr_number) if pr_number is not None else None,
                head_sha=str(head_sha),
                integration_base=str(base),
                slug_valid=valid,
                integration_branch=integration_branch_for(slug) if valid else None,
            )
        )
    return candidates
