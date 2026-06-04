"""ADR-0056 Wave 4: ``role-code-author`` canary schedule + Strategy A retitle.

PR #148 (Task 2) made ``role-prompt-optimizer`` role-agnostic (judge corpus
vs retrospective scorer routed by role type). This task is the Wave 4
canary that wires periodic dispatch for the first non-judge target —
``role-code-author`` — under Strategy A:

  * No new workflow slug. The existing ``wf-tune-judge-prompts`` slug
    becomes role-agnostic; its description is retitled in
    ``services/api/treadmill_api/starters.py`` to reflect that.
  * A new ``SEED_SCHEDULES`` row is added — Sunday 21:00 Pacific
    (``0 21 * * 0``; avoids the 20:00 crystallization tick) — carrying
    ``role_id="role-code-author"`` in its payload.

These assertions pin the contract that the operator deployment step
re-runs ``seed_schedules`` (or one-shot SQL-inserts) against — see the
PR body for the operator's action items.

Validation:
  ``cd services/api && uv run pytest tests/test_wave4_schedules_and_alias.py -q``
"""

from __future__ import annotations

from treadmill_api.seed.schedules import SEED_SCHEDULES
from treadmill_api.starters import STARTERS


# ── SEED_SCHEDULES — role-code-author canary row ─────────────────────────────


def _canary_row() -> dict:
    """The single SEED_SCHEDULES row whose payload carries
    ``role_id == 'role-code-author'``. Lookup is by payload_template
    instead of by ``(workflow_id, cron)`` so a future cron tweak doesn't
    silently mask drift in the rest of the row."""
    matches = [
        s for s in SEED_SCHEDULES
        if s["payload_template"].get("role_id") == "role-code-author"
    ]
    assert len(matches) == 1, (
        f"expected exactly one SEED_SCHEDULES row with "
        f"role_id='role-code-author', got {len(matches)}"
    )
    return matches[0]


def test_role_code_author_payload_carries_repo_and_role_id() -> None:
    """The new row's payload_template must carry ``repo`` (per the
    schedule-payload-needs-repo finding — empty repo silently hangs the
    worker on workspace clone) AND ``role_id`` set to the canary role."""
    payload = _canary_row()["payload_template"]
    assert payload.get("repo"), (
        f"payload_template missing non-empty 'repo' — taskless dispatch "
        f"will hang (schedule-payload-needs-repo). Got: {payload}"
    )
    assert payload.get("role_id") == "role-code-author", (
        f"payload_template['role_id'] must equal 'role-code-author', "
        f"got {payload.get('role_id')!r}"
    )


def test_role_code_author_cron_expression() -> None:
    """Sunday 21:00 Pacific (``0 21 * * 0``) — one hour past the 20:00
    learnings-crystallization tick so the two don't contend."""
    assert _canary_row()["cron_expression"] == "0 21 * * 0"


def test_role_code_author_workflow_id_strategy_a() -> None:
    """Strategy A: no new workflow slug. The canary row reuses
    ``wf-tune-judge-prompts`` (now role-agnostic per ADR-0056 / PR #148).
    Switching slugs would have required a one-shot DB migration to flip
    the existing role-architect schedule onto the new slug — not worth
    the churn for a cosmetic name change."""
    assert _canary_row()["workflow_id"] == "wf-tune-judge-prompts"


# ── starters.py — retitled workflow description ──────────────────────────────


def test_wf_tune_judge_prompts_description_retitled_for_role_agnostic_tuning() -> None:
    """The ``wf-tune-judge-prompts`` STARTERS entry's description must
    reflect Strategy A's role-agnostic intent (judge corpus for JUDGE
    roles + retrospective scorer for AUTHOR / PROCEDURAL roles). The
    slug stays put for schedule-row continuity."""
    wf = next(w for w in STARTERS if w["id"] == "wf-tune-judge-prompts")
    description = wf["description"]
    # The retitle must name both routing branches the role-prompt-optimizer
    # supports per ADR-0056 — otherwise the slug-vs-behavior misnomer keeps
    # growing legs.
    assert "judge" in description.lower(), (
        f"description should still mention judge roles (ADR-0053): "
        f"{description!r}"
    )
    assert "retrospective" in description.lower(), (
        f"description should mention the retrospective scorer branch "
        f"(ADR-0056): {description!r}"
    )
    assert "ADR-0056" in description, (
        f"description should cite ADR-0056 (Strategy A retitle): "
        f"{description!r}"
    )
