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
    """Records git invocations; returns (rc, out) scripted by a matching substring. A LIST
    value is a sequence consumed one per matching call (the last entry repeats) — for the
    push-rejected-then-succeeds retry case."""

    def __init__(self, rc_by_substring: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._script = {
            k: (list(v) if isinstance(v, list) else v)
            for k, v in (rc_by_substring or {}).items()
        }

    async def run(self, *args: str) -> tuple[int, str]:
        self.calls.append(args)
        joined = " ".join(args)
        for sub, result in self._script.items():
            if sub in joined:
                if isinstance(result, list):
                    return result.pop(0) if len(result) > 1 else result[0]
                return result  # type: ignore[return-value]
        return (0, "")

    def ran(self, substring: str) -> bool:
        return any(substring in " ".join(c) for c in self.calls)

    def count(self, substring: str) -> int:
        return sum(1 for c in self.calls if substring in " ".join(c))


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
    # not yet integrated; merge fails with UNMERGED files -> real conflict -> abort, NEVER push.
    r = ScriptedRunner(
        {
            "merge-base --is-ancestor": (1, ""),
            "merge --no-ff --no-edit": (1, "CONFLICT"),
            "ls-files -u": (0, "100644 abc 1\tsrc/x.py"),  # unmerged files present
        }
    )
    assert await integrate_task(r, OP) == "conflict"
    assert r.ran("git merge --abort")
    assert not r.ran("git push")
    assert not r.ran("--force")


@pytest.mark.asyncio
async def test_infra_merge_failure_is_not_a_conflict():
    # merge fails but NO unmerged files (missing object / dirty tree) -> infra error, not a
    # conflict worker (Bert non-blocking #1).
    r = ScriptedRunner(
        {
            "merge-base --is-ancestor": (1, ""),
            "merge --no-ff --no-edit": (128, "fatal: not something we can merge"),
            "ls-files -u": (0, ""),  # nothing unmerged -> not a conflict
        }
    )
    assert await integrate_task(r, OP) == "merge-failed"
    assert r.ran("git merge --abort")
    assert not r.ran("git push")


@pytest.mark.asyncio
async def test_fetch_failure_never_reasons_on_stale_refs():
    r = ScriptedRunner({"git fetch": (1, "network down")})
    assert await integrate_task(r, OP) == "fetch-failed"
    assert not r.ran("merge-base")  # never reasons after a failed fetch
    assert not r.ran("git push")


@pytest.mark.asyncio
async def test_push_rejected_retries_then_succeeds():
    # THE BLOCKING CASE (Bert): origin advances between fetch and push -> non-fast-forward
    # rejection. The push rc must be checked and the cycle re-driven; the ancestry check keeps
    # the retry safe. Push fails once, then lands.
    r = ScriptedRunner(
        {"merge-base --is-ancestor": (1, ""), "git push": [(1, "rejected: non-fast-forward"), (0, "")]}
    )
    assert await integrate_task(r, OP) == "merged"
    assert r.count("git push") == 2  # re-driven, not silently reported merged
    assert not r.ran("--force")


@pytest.mark.asyncio
async def test_push_rejected_exhausts_returns_push_rejected_not_merged():
    # origin keeps advancing past max_retries -> NEVER return "merged"; caller re-drives.
    r = ScriptedRunner({"merge-base --is-ancestor": (1, ""), "git push": (1, "rejected")})
    assert await integrate_task(r, OP, max_retries=2) == "push-rejected"
    assert r.count("git push") == 3  # max_retries + 1 attempts


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


# ── PR-ref fetch + tip verification (ADR-0119 TOCTOU guard, Bert #421) ─────────

OP_PR = MergeOp(
    repo="joeLepper/treadmill",
    task_head="deadbeef" * 5,
    integration_branch="joes-agents/my-plan",
    base="main",
    pr_number=7,
)


@pytest.mark.asyncio
async def test_pr_ref_verified_tip_matches_then_merges():
    # fetch refs/pull/7/head; FETCH_HEAD tip == approved head -> proceed and merge.
    r = ScriptedRunner(
        {"merge-base --is-ancestor": (1, ""), "rev-parse FETCH_HEAD": (0, OP_PR.task_head)}
    )
    assert await integrate_task(r, OP_PR) == "merged"
    # the PR ref is fetched ALONE (so rev-parse FETCH_HEAD names the PR tip, not the branch),
    # then the branch is fetched separately.
    assert r.ran("git fetch origin refs/pull/7/head")
    assert r.ran("git fetch origin joes-agents/my-plan")
    assert not r.ran("git fetch origin joes-agents/my-plan refs/pull/7/head")  # never combined
    assert r.ran("git rev-parse FETCH_HEAD")
    assert r.ran("git merge --no-ff --no-edit " + OP_PR.task_head)


@pytest.mark.asyncio
async def test_pr_ref_tip_moved_refuses_to_merge():
    # the PR head moved since approval -> FETCH_HEAD tip != approved head -> head-moved, NO merge.
    r = ScriptedRunner({"rev-parse FETCH_HEAD": (0, "f00dbabe" * 5)})  # different sha
    assert await integrate_task(r, OP_PR) == "head-moved"
    assert not r.ran("git merge --no-ff")  # never merged unapproved content
    assert not r.ran("git push")


@pytest.mark.asyncio
async def test_pr_ref_fetch_failure_is_infra():
    r = ScriptedRunner({"refs/pull/7/head": (1, "network")})
    assert await integrate_task(r, OP_PR) == "fetch-failed"
    assert not r.ran("git merge --no-ff")
