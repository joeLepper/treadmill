"""Foils for the ADR-0116 gate-position decision (Team A / bert).

RED-then-GREEN: each pins a discriminating case so a naive rule ("always heavy", or
"feature-branch -> always light", or "unknown -> light") goes RED. The pure ``gate_target``
runs DB-free; the wrapper uses a scripted stub session.
"""

from __future__ import annotations

import pytest
from treadmill_api.coordination.gate_position import (
    HEAVY,
    LIGHT,
    gate_target,
    gate_weight_for_repo,
)


def test_feature_branch_task_pr_is_light():
    w = gate_target("feature-branch")
    assert w is LIGHT
    # pin the actual weights — a wrong panel breadth / CI flag must RED here.
    assert (w.min_cross_model, w.run_ci, w.label) == (1, False, "light")


def test_feature_branch_promotion_is_heavy():
    # The discriminating case: a naive "feature-branch -> light" would MISS the heavy promotion.
    w = gate_target("feature-branch", is_promotion=True)
    assert w is HEAVY
    assert (w.min_cross_model, w.run_ci, w.label) == (2, True, "heavy")


def test_main_is_heavy_regardless_of_promotion_flag():
    assert gate_target("main") is HEAVY
    assert gate_target("main", is_promotion=True) is HEAVY  # main is always the full gate


def test_unknown_merge_target_raises_not_silently_light():
    # Fail closed: an unrecognized target must NOT under-gate to LIGHT.
    with pytest.raises(ValueError):
        gate_target("trunk")


# ── wrapper (scripted stub session) ───────────────────────────────────────────


class _Cfg:
    def __init__(self, merge_target):
        self.merge_target = merge_target


class _StubSession:
    """Returns a scripted value for ``scalar`` (TeamConfigStore.get_by_repo uses it)."""

    def __init__(self, value):
        self._value = value

    async def scalar(self, *_a, **_k):
        return self._value


@pytest.mark.asyncio
async def test_wrapper_reads_main_config_as_heavy():
    w = await gate_weight_for_repo(_StubSession(_Cfg("main")), "some/repo")
    assert w is HEAVY


@pytest.mark.asyncio
async def test_wrapper_reads_feature_branch_config_as_light():
    w = await gate_weight_for_repo(_StubSession(_Cfg("feature-branch")), "some/repo")
    assert w is LIGHT


@pytest.mark.asyncio
async def test_wrapper_absent_config_defaults_to_feature_branch_light():
    # No team_configs row -> the feature-branch column default -> LIGHT (not an error, not heavy).
    w = await gate_weight_for_repo(_StubSession(None), "unconfigured/repo")
    assert w is LIGHT


@pytest.mark.asyncio
async def test_wrapper_promotion_on_feature_branch_repo_is_heavy():
    w = await gate_weight_for_repo(
        _StubSession(_Cfg("feature-branch")), "some/repo", is_promotion=True
    )
    assert w is HEAVY
