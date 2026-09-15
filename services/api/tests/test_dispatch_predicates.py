"""Foils for the ADR-0118 dispatch predicates (Team A / bert).

Each test is RED-then-GREEN: it pins the exact discriminating behavior of a predicate, so a
naive implementation (first-segment matching, "any non-success -> not ready", a task-only or
head-blind key) goes RED and the correct one GREEN. The pure cores run DB-free; the async
wrappers use a scripted stub session, the ``test_ci_observer`` pattern.
"""

from __future__ import annotations

import uuid

import pytest
from treadmill_api.coordination.dispatch_predicates import (
    CheckResult,
    TerminalFact,
    _head_sha_of,
    ci_ready,
    depends_on_satisfied,
    is_ci_ready,
    is_depends_on_satisfied,
    on_ci_result,
)

X = str(uuid.uuid4())
Y = str(uuid.uuid4())


def _pr_merged(expr_task=X):
    return f"task.{expr_task}.pr_merged"


# ── depends_on_satisfied (pure) ───────────────────────────────────────────────


def test_no_dependencies_is_vacuously_satisfied():
    assert depends_on_satisfied([], []) is True


def test_pr_merged_edge_red_then_green():
    expr = _pr_merged(X)
    # RED: the upstream has not merged -> not satisfied.
    assert depends_on_satisfied([expr], []) is False
    # GREEN: the pr_merged terminal fact for X exists -> satisfied.
    assert depends_on_satisfied([expr], [TerminalFact(X, "pr_merged")]) is True


def test_run_completed_edge():
    expr = f"task.{X}.run.completed"
    assert depends_on_satisfied([expr], []) is False
    assert depends_on_satisfied([expr], [TerminalFact(X, "run.completed")]) is True


def test_step_edge_matches_only_the_named_step():
    expr = f"task.{X}.step.build.completed"
    # A DIFFERENT step's completion must NOT satisfy this edge (the discriminating case).
    assert depends_on_satisfied([expr], [TerminalFact(X, "step.completed", "test")]) is False
    assert depends_on_satisfied([expr], [TerminalFact(X, "step.completed", "build")]) is True


def test_multiple_edges_are_anded():
    exprs = [_pr_merged(X), _pr_merged(Y)]
    assert depends_on_satisfied(exprs, [TerminalFact(X, "pr_merged")]) is False  # one missing
    assert (
        depends_on_satisfied(exprs, [TerminalFact(X, "pr_merged"), TerminalFact(Y, "pr_merged")])
        is True
    )


def test_latching_edge_survives_upstream_rework():
    """Alan's foil: X merges -> satisfied; X reworks (new events land) -> STILL satisfied.

    Terminal facts are append-only, so the pr_merged fact persists even as X accrues new
    activity. The dependent must not re-block.
    """
    expr = _pr_merged(X)
    facts = {TerminalFact(X, "pr_merged")}
    assert depends_on_satisfied([expr], facts) is True
    # X reworks: more facts arrive, the pr_merged fact is NOT removed.
    facts |= {TerminalFact(X, "step.completed", "rework-verify")}
    assert depends_on_satisfied([expr], facts) is True


def test_malformed_expression_raises_not_silently_passes():
    with pytest.raises(ValueError):
        depends_on_satisfied(["task.not-a-uuid.pr_merged"], [])


# ── ci_ready (pure) ───────────────────────────────────────────────────────────

REQ = {"github-actions"}


def test_all_required_success_is_ready():
    assert ci_ready([CheckResult("github-actions", "success")], REQ) is True


def test_required_failure_is_not_ready():
    assert ci_ready([CheckResult("github-actions", "failure")], REQ) is False


def test_required_non_terminal_is_not_ready():
    # conclusion=None models an in-progress required check: not ready, but see unstable foil
    # for why we must not then wait-forever on a NON-required one.
    assert ci_ready([CheckResult("github-actions", None)], REQ) is False


def test_no_results_is_not_ready():
    assert ci_ready([], REQ) is False


def test_unstable_foil_non_required_neutral_or_skipped_does_not_block():
    """The #22845 case. Required is green; a NON-required suite is neutral/skipped
    (mergeable_state would read ``unstable``). A naive "any non-success -> not ready" goes
    RED here; the correct predicate ignores non-required -> GREEN.
    """
    results = [
        CheckResult("github-actions", "success"),  # required, green
        CheckResult("kodiak", "neutral"),  # non-required
        CheckResult("netlify", "skipped"),  # non-required
    ]
    assert ci_ready(results, REQ) is True


def test_non_required_failure_never_blocks():
    results = [
        CheckResult("github-actions", "success"),  # required, green
        CheckResult("optional-lint", "failure"),  # non-required, failed
    ]
    assert ci_ready(results, REQ) is True


def test_missing_one_of_several_required_is_not_ready():
    req = {"github-actions", "e2e"}
    results = [CheckResult("github-actions", "success")]  # e2e absent
    assert ci_ready(results, req) is False


def test_empty_required_is_ready_documented():
    # Nothing gates. The wrapper must never pass an empty set to mean "unknown".
    assert ci_ready([CheckResult("anything", "failure")], set()) is True


# ── async wrappers (scripted stub session) ────────────────────────────────────


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _StubSession:
    """Returns scripted row lists for successive ``execute`` calls."""

    def __init__(self, *row_batches):
        self._batches = list(row_batches)
        self._i = 0

    async def execute(self, *_a, **_k):
        batch = self._batches[self._i]
        self._i += 1
        return _Result(batch)


@pytest.mark.asyncio
async def test_wrapper_no_deps_is_satisfied_without_fact_lookup():
    session = _StubSession([])  # zero dependency rows
    assert await is_depends_on_satisfied(session, X) is True


@pytest.mark.asyncio
async def test_wrapper_pr_merged_present_is_satisfied():
    # dep rows, then event rows (task_id, entity_type, action, payload)
    session = _StubSession(
        [(_pr_merged(X),)],
        [(uuid.UUID(X), "github", "pr_merged", {})],
    )
    assert await is_depends_on_satisfied(session, "dependent-task") is True


@pytest.mark.asyncio
async def test_wrapper_generation_does_not_change_satisfaction():
    """Satisfaction is generation-independent; the generation arg is call-site symmetry only.
    A task-only vs generation-scoped satisfaction bug would diverge here.
    """
    facts = [(uuid.UUID(X), "github", "pr_merged", {})]
    s1 = _StubSession([(_pr_merged(X),)], facts)
    s5 = _StubSession([(_pr_merged(X),)], list(facts))
    assert await is_depends_on_satisfied(s1, "t", generation=1) is True
    assert await is_depends_on_satisfied(s5, "t", generation=5) is True


@pytest.mark.asyncio
async def test_wrapper_ci_ready_maps_payloads_and_applies_required():
    session = _StubSession(
        [
            ({"app_slug": "github-actions", "conclusion": "success"},),
            ({"app_slug": "kodiak", "conclusion": "neutral"},),  # non-required, ignored
        ]
    )
    assert await is_ci_ready(session, "deadbeef", {"github-actions"}) is True


@pytest.mark.asyncio
async def test_wrapper_ci_not_ready_when_required_failed():
    session = _StubSession([({"app_slug": "github-actions", "conclusion": "failure"},)])
    assert await is_ci_ready(session, "deadbeef", {"github-actions"}) is False


# Head isolation is enforced structurally by the ``commit_sha == head_sha`` filter in
# is_ci_ready's query; a real-DB integration test (alan's consumer suite) covers that a
# ci_result for a superseded head is not fetched. The unit tests above cover the payload
# mapping + the required-set decision.


# ── on_ci_result handler (decision; write injected) ───────────────────────────


class _EvalSpy:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, task_id, head_sha):
        self.calls.append((task_id, head_sha))


def _ci_record(task_id="dep-task", head="abc123", *, payload=None):
    return {
        "task_id": task_id,
        "action": "ci_result",
        "entity_type": "task",
        "payload": payload if payload is not None else {"head_sha": head},
    }


def test_head_sha_of_dict_json_and_missing():
    assert _head_sha_of(_ci_record(head="H1")) == "H1"
    assert _head_sha_of(_ci_record(payload='{"head_sha": "H2"}')) == "H2"
    assert _head_sha_of({"payload": {}}) is None
    assert _head_sha_of({"payload": "not json"}) is None


@pytest.mark.asyncio
async def test_on_ci_result_dispatches_evaluator_when_ready():
    # is_ci_ready query returns a required suite success -> ready.
    session = _StubSession([({"app_slug": "github-actions", "conclusion": "success"},)])
    spy = _EvalSpy()
    dispatched = await on_ci_result(
        session, _ci_record("t1", "HEAD"), spy, required={"github-actions"}
    )
    assert dispatched is True
    assert spy.calls == [("t1", "HEAD")]


@pytest.mark.asyncio
async def test_on_ci_result_no_dispatch_when_required_failed():
    session = _StubSession([({"app_slug": "github-actions", "conclusion": "failure"},)])
    spy = _EvalSpy()
    dispatched = await on_ci_result(
        session, _ci_record("t1", "HEAD"), spy, required={"github-actions"}
    )
    assert dispatched is False
    assert spy.calls == []


@pytest.mark.asyncio
async def test_on_ci_result_no_dispatch_when_head_or_task_missing():
    spy = _EvalSpy()
    # missing head_sha: no session query needed, must not dispatch
    assert await on_ci_result(_StubSession(), {"task_id": "t", "payload": {}}, spy) is False
    # missing task_id
    assert await on_ci_result(_StubSession(), {"payload": {"head_sha": "H"}}, spy) is False
    assert spy.calls == []


@pytest.mark.asyncio
async def test_on_ci_result_does_not_dedup_writes_must():
    """on_ci_result is a DECISION: a later ci_result for the same head (a trailing suite) is
    still 'ready', so it dispatches AGAIN. The at-most-once-per-head guard is the WRITE's
    job (dispatch_evaluator), documented so the consumer owns it — this pins that split.
    """
    spy = _EvalSpy()
    for _ in range(2):
        session = _StubSession([({"app_slug": "github-actions", "conclusion": "success"},)])
        await on_ci_result(session, _ci_record("t1", "HEAD"), spy, required={"github-actions"})
    assert spy.calls == [("t1", "HEAD"), ("t1", "HEAD")]  # decision fires each time; write dedups
