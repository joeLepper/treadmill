"""Per-state drain-guard classification foils (ADR-0109).

Pure unit tests of `_classify` — the fail-closed per-state mapping the team-down
drain-guard uses. This pins WHICH states block teardown vs allow vs park, so a
regression that (e.g.) treats `registered` as terminal, or drops the escalated→parked
rule, goes red here. The SQL that FEEDS this (the task_status + escalation CTE join and
the post-merge deploy check) is verified separately against a real DB — this file owns
the classification logic, which is where the "checks only executing" gap would hide.
"""
from treadmill_api.routers.team_configs import _classify, _is_merged, _is_terminal


def test_team_active_states_block() -> None:
    # Every non-terminal, non-escalated state is TEAM-ACTIVE → blocks teardown.
    for ds in [
        "registered",
        "pr_opened",              # an open PR is team-active
        "wf-quick: executing",   # per-worker executing
        "review_passed",         # reviewed but not merged
        "blocked-on-ci",         # blocked (on a dependency/CI) is still team work
        "drafting",
        "changes_requested",     # rework
    ]:
        assert _classify(ds, escalated=False) == "block", ds


def test_terminal_non_merged_is_clean() -> None:
    for ds in ["done", "validated", "cancelled"]:
        assert _classify(ds, escalated=False) == "clean", ds


def test_merged_needs_deploy_check() -> None:
    # pr_merged is terminal but must go through the post-merge deploy check.
    assert _classify("pr_merged", escalated=False) == "merged"
    assert _classify("pr_merged worker-slug-1", escalated=False) == "merged"  # prefixed


def test_escalated_is_parked_not_blocking() -> None:
    # Parked-on-human overrides everything: escalated never blocks (even mid-execution
    # or merged) — it is tracked for re-standup on the operator's response.
    assert _classify("wf-x: executing", escalated=True) == "parked"
    assert _classify("registered", escalated=True) == "parked"
    assert _classify("pr_merged", escalated=True) == "parked"


def test_unknown_state_fails_closed() -> None:
    # Fail-closed: an unrecognized or NULL derived_status is not terminal → blocks,
    # so a new state added to the view can never silently let a team tear down.
    assert _classify("some-brand-new-state", escalated=False) == "block"
    assert _classify(None, escalated=False) == "block"


def test_terminal_and_merged_predicates() -> None:
    assert _is_terminal("pr_merged") and _is_terminal("pr_merged x")
    assert _is_terminal("done") and _is_terminal("cancelled") and _is_terminal("validated")
    assert not _is_terminal("registered") and not _is_terminal(None)
    assert _is_merged("pr_merged") and _is_merged("pr_merged x")
    assert not _is_merged("done")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("PASS: per-state drain-guard classification")
