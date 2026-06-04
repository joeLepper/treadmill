"""Structural test for the role-prompt-optimizer's routing logic.

Per ADR-0056 (prompt tuning is role-agnostic via pluggable metrics),
the role-prompt-optimizer prompt must branch on the target role's type
and call the right scorer:

  - JUDGE roles  → ``evaluate_judge_prompt`` (ADR-0053 Wave 2)
  - AUTHOR / PROCEDURAL roles → ``evaluate_role_retrospectively`` (ADR-0056)

This test loads the role definition from ``starters.py`` and asserts
the prompt text mentions BOTH scorer names. It catches drift where a
future prompt edit accidentally drops one of the routing branches (the
operator's intent was lost, the optimizer would silently mis-score the
"other" role family).

Validation:
  ``cd services/api && uv run pytest tests/test_optimizer_prompt_routing.py -q``
"""

from __future__ import annotations

from treadmill_api.starters import _ROLES_BY_ID


def _optimizer_prompt() -> str:
    role = _ROLES_BY_ID["role-prompt-optimizer"]
    return role["system_prompt"]


def test_prompt_mentions_judge_scorer() -> None:
    """JUDGE branch must call ``evaluate_judge_prompt`` (ADR-0053 Wave 2)."""
    assert "evaluate_judge_prompt" in _optimizer_prompt(), (
        "role-prompt-optimizer's prompt must mention "
        "``evaluate_judge_prompt`` so the JUDGE routing branch survives "
        "future edits (ADR-0053 Wave 2 / ADR-0056)"
    )


def test_prompt_mentions_retrospective_scorer() -> None:
    """AUTHOR/PROCEDURAL branch must call ``evaluate_role_retrospectively``
    (ADR-0056)."""
    assert "evaluate_role_retrospectively" in _optimizer_prompt(), (
        "role-prompt-optimizer's prompt must mention "
        "``evaluate_role_retrospectively`` so the AUTHOR/PROCEDURAL "
        "routing branch survives future edits (ADR-0056)"
    )


def test_prompt_mentions_both_scorers_together() -> None:
    """Belt-and-suspenders: a single assertion the operator can scan
    for that proves the routing logic is intact."""
    prompt = _optimizer_prompt()
    assert (
        "evaluate_judge_prompt" in prompt
        and "evaluate_role_retrospectively" in prompt
    ), (
        "role-prompt-optimizer's prompt has lost one of its two scorer "
        "calls — the optimizer can no longer route by role type. Restore "
        "both ``evaluate_judge_prompt`` (JUDGE) and "
        "``evaluate_role_retrospectively`` (AUTHOR/PROCEDURAL)."
    )
