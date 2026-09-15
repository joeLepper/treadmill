"""Gate-position by merge_target — ADR-0116, for the ADR-0118 router (Team A / bert).

When the router invokes the evaluator per PR, the gate WEIGHT must match the blast radius:
a task PR into a per-plan integration branch is not `main`, so it gets a light gate; the
change reaches `main` only at the human-owned branch->main promotion (or, for a repo that
permits agent-merge-to-main, per task). This is the pure decision the router consults to set
the evaluator's panel breadth and whether the full CI matrix gates the PR.

ADR-0116 decision, verbatim mapping (``merge_target`` is ``feature-branch`` | ``main``):
* ``feature-branch`` TASK PR  -> LIGHT: single cross-model pass (``--min-cross-model 1``),
  NO full CI (ADR-0115 — CI belongs on the human-owned branch->main).
* ``feature-branch`` PROMOTION (integration->main) -> HEAVY, once: full panel
  (``--min-cross-model 2``) + full CI. Human-owned today; the WEIGHT is heavy wherever it runs.
* ``main`` -> HEAVY: full panel + CI per task (the rare agent-merge-to-main repo; unchanged).

The pure ``gate_target`` is the decision; ``gate_weight_for_repo`` reads the repo's
``team_configs.merge_target`` (default ``feature-branch``) and applies it.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.team_config_store import TeamConfigStore

# The column default (models/team_config.py): a repo with no config row is feature-branch,
# the safe common case where agent-merge-to-main is blocked.
DEFAULT_MERGE_TARGET = "feature-branch"


@dataclass(frozen=True)
class GateWeight:
    """The gate configuration for a PR. ``min_cross_model`` is the evaluator panel breadth
    (1 = one cross-family voice; 2 = full panel); ``run_ci`` is whether the full CI matrix
    gates this PR; ``label`` names the weight for logs/traces."""

    min_cross_model: int
    run_ci: bool
    label: str


LIGHT = GateWeight(min_cross_model=1, run_ci=False, label="light")
HEAVY = GateWeight(min_cross_model=2, run_ci=True, label="heavy")


def gate_target(merge_target: str, *, is_promotion: bool = False) -> GateWeight:
    """The ADR-0116 gate weight for a PR, by its repo's ``merge_target`` and whether it is the
    integration->main promotion.

    Raises ``ValueError`` on an unknown ``merge_target`` — a gate must never silently fall to
    LIGHT on a value it does not understand (that would under-gate a change reaching ``main``).
    """
    if merge_target == "main":
        return HEAVY  # agent-merge-to-main: full panel + CI per task, unchanged.
    if merge_target == "feature-branch":
        # A task PR into the integration branch is light; the promotion to main is heavy once.
        return HEAVY if is_promotion else LIGHT
    raise ValueError(f"unknown merge_target: {merge_target!r}")


async def gate_weight_for_repo(
    session: AsyncSession, repo: str, *, is_promotion: bool = False
) -> GateWeight:
    """Resolve the gate weight for ``repo``: read its ``team_configs.merge_target`` (absent →
    the ``feature-branch`` default) and apply ``gate_target``."""
    cfg = await TeamConfigStore().get_by_repo(session, repo)
    merge_target = getattr(cfg, "merge_target", None) or DEFAULT_MERGE_TARGET
    return gate_target(merge_target, is_promotion=is_promotion)
