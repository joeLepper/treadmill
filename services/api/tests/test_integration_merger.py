"""Foils for the ADR-0118 phase-2 integration merger (alan).

Each pins a discriminating behavior through a SCRIPTED GitRunner (records commands, returns
scripted returncodes) — no live git. RED-then-GREEN: a naive "always merge+push" fails the
idempotency + conflict + base-safety cases.
"""

from __future__ import annotations

import pytest

from treadmill_api.coordination.integration_merger import (
    MergeOp,
    drift,
    integrate_task,
    integration_branch_for,
)


class ScriptedRunner:
    """Records git invocations; returns (rc, out) scripted by a matching substring."""

    def __init__(self, rc_by_substring: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._script = rc_by_substring or {}

    async def run(self, *args: str) -> tuple[int, str]:
        self.calls.append(args)
        joined = " ".join(args)
        for sub, result in self._script.items():
            if sub in joined:
                return result
        return (0, "")

    def ran(self, substring: str) -> bool:
        return any(substring in " ".join(c) for c in self.calls)


OP = MergeOp(
    repo="joeLepper/treadmill",
    task_head="deadbeef" * 5,
    integration_branch="joes-agents/my-plan",
    base="main",
)


def test_integration_branch_naming():
    assert integration_branch_for("my-plan") == "joes-agents/my-plan"


@pytest.mark.asyncio
async def test_new_head_is_merged_and_pushed():
    # ancestor check RED (rc=1 -> not yet integrated); merge clean.
    r = ScriptedRunner({"merge-base --is-ancestor": (1, "")})
    assert await integrate_task(r, OP) == "merged"
    assert r.ran("git merge --no-ff --no-edit " + OP.task_head)
    assert r.ran("git push origin joes-agents/my-plan")


@pytest.mark.asyncio
async def test_already_integrated_is_a_noop_no_push():
    # ATOMICITY: head already an ancestor (rc=0) -> no-op; a retry after a half-landed push,
    # or a duplicate approve, must NOT merge or push again.
    r = ScriptedRunner({"merge-base --is-ancestor": (0, "")})
    assert await integrate_task(r, OP) == "already-integrated"
    assert not r.ran("git merge --no-ff")  # never re-merges
    assert not r.ran("git push")  # never double-pushes


@pytest.mark.asyncio
async def test_conflict_aborts_and_never_pushes():
    # not yet integrated, but the merge conflicts -> abort, signal, NEVER push/force.
    r = ScriptedRunner(
        {"merge-base --is-ancestor": (1, ""), "merge --no-ff --no-edit": (1, "CONFLICT")}
    )
    assert await integrate_task(r, OP) == "conflict"
    assert r.ran("git merge --abort")
    assert not r.ran("git push")
    assert not r.ran("--force")


@pytest.mark.asyncio
async def test_drift_up_to_date_when_base_is_ancestor():
    r = ScriptedRunner({"merge-base --is-ancestor": (0, "")})
    assert await drift(r, "joes-agents/my-plan", "main") == "up-to-date"
    assert not r.ran("git push")


@pytest.mark.asyncio
async def test_drift_against_a_non_main_base_never_merges_main():
    # ADR-0114: a non-main base must drift against THAT ref, never main.
    base = "joes-agents/run-shape-telemetry-design"
    r = ScriptedRunner({"merge-base --is-ancestor": (1, "")})
    assert await drift(r, "joes-agents/my-plan", base) == "drifted"
    assert r.ran(f"git merge --no-ff --no-edit origin/{base}")
    # the only 'main' allowed is inside the plan slug, never a `origin/main` merge:
    assert not r.ran("merge --no-ff --no-edit origin/main")
