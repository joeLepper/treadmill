"""ADR-0056 Wave 4 — three additional role-tuning schedules + the
workflow-slug rename.

Two things land together in Wave 4:

  1. ``wf-tune-role-prompts`` becomes the canonical workflow slug. The
     pre-rename ``wf-tune-judge-prompts`` stays registered (same role +
     step config) so the role-architect schedule registered before
     ADR-0056 still resolves on dispatch. The optimizer prompt accepts
     both ``role_id`` (the new payload key) and ``judge_role`` (the
     legacy synonym) so the existing schedule row keeps working.

  2. Three new ``SEED_SCHEDULES`` rows under ``wf-tune-role-prompts``
     extend retrospective tuning (ADR-0056) to the three highest-value
     non-architect roles:

       * role-code-author — Sunday 21:00 Pacific (NOT 20:00; that
         collides with the wf-crystallize-learning sweep on the same
         tick — both heavy weekly jobs, both single-tenant on the
         worker pool, no need to race them).
       * role-reviewer   — Monday 21:00 Pacific.
       * role-validator  — Tuesday 21:00 Pacific.

This module pins the two together: starters must register both slugs,
seed must carry the four scheduled tuning rows with the right
identifiers (the architect row keys on the legacy ``judge_role`` for
backward compat; the three Wave 4 rows key on the new ``role_id``).
"""

from __future__ import annotations

from treadmill_api.seed.schedules import SEED_SCHEDULES
from treadmill_api.starters import STARTERS


# ── starters: both workflow slugs present ────────────────────────────────────


def test_starters_registers_both_tune_workflow_slugs() -> None:
    """Wave 4 keeps the old slug registered as a deprecated alias so the
    pre-rename role-architect schedule still resolves on dispatch."""
    ids = {wf["id"] for wf in STARTERS}
    assert "wf-tune-role-prompts" in ids, (
        "ADR-0056 Wave 4: wf-tune-role-prompts is the canonical "
        "successor slug — it must be present in STARTERS."
    )
    assert "wf-tune-judge-prompts" in ids, (
        "ADR-0056 Wave 4: wf-tune-judge-prompts MUST stay registered "
        "as a deprecated alias so the role-architect schedule row "
        "registered before the rename still resolves on dispatch."
    )


def test_both_tune_slugs_share_role_and_step_config() -> None:
    """The deprecated alias and the canonical slug point at the same
    role + step config — same single ``optimize`` step on the same
    ``role-prompt-optimizer``. If they diverged, dispatching the legacy
    row would do something different from dispatching the new row,
    silently breaking the architect schedule."""
    by_id = {wf["id"]: wf for wf in STARTERS}
    old = by_id["wf-tune-judge-prompts"]
    new = by_id["wf-tune-role-prompts"]
    assert old["steps"] == new["steps"], (
        f"alias drift: old steps {old['steps']!r} != new steps {new['steps']!r}"
    )
    old_role_ids = [r["id"] for r in old["roles"]]
    new_role_ids = [r["id"] for r in new["roles"]]
    assert old_role_ids == new_role_ids == ["role-prompt-optimizer"], (
        f"alias drift: roles must both be [role-prompt-optimizer]; "
        f"old={old_role_ids!r}, new={new_role_ids!r}"
    )


def test_deprecated_alias_description_names_successor() -> None:
    """The alias must announce itself as deprecated AND name its
    successor — operators reading the seeded workflow list otherwise
    can't tell why two slugs map to the same role."""
    by_id = {wf["id"]: wf for wf in STARTERS}
    desc = by_id["wf-tune-judge-prompts"]["description"].lower()
    assert "deprecated" in desc, (
        f"wf-tune-judge-prompts description must announce deprecation: "
        f"{by_id['wf-tune-judge-prompts']['description']!r}"
    )
    assert "wf-tune-role-prompts" in desc, (
        f"wf-tune-judge-prompts description must name its successor "
        f"slug: {by_id['wf-tune-judge-prompts']['description']!r}"
    )


# ── seed: four tuning schedules with the right ids ───────────────────────────


def _tuning_schedules() -> list[dict]:
    """All seed rows that dispatch a tuning run — either via the
    canonical slug or the deprecated alias."""
    return [
        s for s in SEED_SCHEDULES
        if s["workflow_id"] in ("wf-tune-role-prompts", "wf-tune-judge-prompts")
    ]


def test_four_tuning_schedules_present() -> None:
    """One pre-Wave-4 architect schedule (under the legacy slug) plus
    three Wave 4 schedules (under the new slug) = four total tuning
    runs across the seeded cron landscape."""
    assert len(_tuning_schedules()) == 4, (
        f"expected 4 tuning schedules (1 architect + 3 Wave 4), "
        f"got {len(_tuning_schedules())}: "
        f"{[(s['workflow_id'], s['cron_expression']) for s in _tuning_schedules()]}"
    )


def test_architect_schedule_keeps_legacy_judge_role_key() -> None:
    """The pre-rename architect schedule still uses ``judge_role`` (not
    ``role_id``) — the optimizer prompt accepts both for backward
    compat, and rewriting the seed row would invalidate the existing
    DB row on populated deployments (``seed_schedules`` deduplicates on
    ``(workflow_id, cron_expression)``, not on payload contents)."""
    matches = [
        s for s in SEED_SCHEDULES
        if s["workflow_id"] == "wf-tune-judge-prompts"
    ]
    assert len(matches) == 1, (
        f"expected exactly one wf-tune-judge-prompts schedule "
        f"(the architect row); got {len(matches)}"
    )
    payload = matches[0]["payload_template"]
    assert payload.get("judge_role") == "role-architect", (
        f"architect schedule must keep its legacy ``judge_role`` key "
        f"so the pre-rename DB row still resolves; got payload {payload!r}"
    )


def test_wave4_schedules_use_new_role_id_key() -> None:
    """The three Wave 4 schedules under ``wf-tune-role-prompts`` use
    ``role_id`` — the new ADR-0056 payload key that replaces
    ``judge_role``. Each names a distinct target role."""
    rows = [
        s for s in SEED_SCHEDULES
        if s["workflow_id"] == "wf-tune-role-prompts"
    ]
    assert len(rows) == 3, (
        f"expected 3 wf-tune-role-prompts schedules (code-author, "
        f"reviewer, validator); got {len(rows)}"
    )
    role_ids = sorted(s["payload_template"]["role_id"] for s in rows)
    assert role_ids == [
        "role-code-author",
        "role-reviewer",
        "role-validator",
    ], f"Wave 4 schedules must target these three roles; got {role_ids!r}"
    # None of the Wave 4 rows should leak the legacy key.
    for s in rows:
        payload = s["payload_template"]
        assert "judge_role" not in payload, (
            f"Wave 4 schedules must use ``role_id`` only; row "
            f"{s['cron_expression']!r} also carries legacy "
            f"``judge_role={payload['judge_role']!r}``"
        )


def test_wave4_schedules_have_expected_crons() -> None:
    """Sun/Mon/Tue 21:00 Pacific. The Sunday 21:00 choice (not 20:00)
    avoids the same-tick race with wf-crystallize-learning."""
    by_role = {
        s["payload_template"]["role_id"]: s["cron_expression"]
        for s in SEED_SCHEDULES
        if s["workflow_id"] == "wf-tune-role-prompts"
    }
    assert by_role["role-code-author"] == "0 21 * * 0", (
        "role-code-author must run Sunday 21:00 Pacific — Sunday 20:00 "
        "collides with wf-crystallize-learning on the same tick."
    )
    assert by_role["role-reviewer"] == "0 21 * * 1"
    assert by_role["role-validator"] == "0 21 * * 2"


def test_wave4_schedules_carry_repo() -> None:
    """The schedule-payload-needs-repo trap — taskless dispatch reads
    ``rendered_payload["repo"]`` for the worker workspace, and an
    empty value silently hangs the step pending forever. All four
    tuning rows (architect + Wave 4) must carry a non-empty repo."""
    for s in _tuning_schedules():
        payload = s["payload_template"]
        assert payload.get("repo"), (
            f"tuning schedule for {s['workflow_id']!r} cron "
            f"{s['cron_expression']!r} missing non-empty 'repo' — "
            f"taskless dispatch will hang the step"
        )
